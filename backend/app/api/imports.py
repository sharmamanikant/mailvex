from __future__ import annotations

import csv
import io
import json
import logging
from datetime import UTC, datetime
from pathlib import Path
from uuid import UUID, uuid4

from fastapi import APIRouter, Depends, File, HTTPException, UploadFile, status
from fastapi.responses import StreamingResponse
from pydantic import BaseModel
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.billing import EVENT_STORAGE_USED, UsageService
from app.core.config import settings
from app.core.database import get_db
from app.models import ImportJob
from app.security.permissions import TenantPrincipal, require_permission
from app.services.contact_fields import ContactFieldService
from app.services.imports import (
    DEDUP_POLICIES,
    MAX_FILE_BYTES,
    STANDARD_FIELDS,
    ImportService,
    ImportValidationError,
)
from app.workers.imports import (
    TERMINAL_JOB_STATES,
    QueueUnavailable,
    enqueue_import,
    enqueue_validate,
)

router = APIRouter(prefix="/contacts/imports", tags=["contact-imports"])
UPLOAD_DIR = (Path(settings.storage_root).expanduser() / "imports").resolve()
STUCK_JOB_AFTER_SECONDS = 30
logger = logging.getLogger(__name__)


class ImportJobUpdate(BaseModel):
    column_mapping: dict[str, str] | None = None
    duplicate_policy: str | None = None


def _job_dto(job: ImportJob) -> dict[str, object]:
    return {
        "id": str(job.id),
        "status": job.status,
        "filename": job.filename,
        "file_type": job.file_type,
        "column_mapping": job.column_mapping,
        "duplicate_policy": job.duplicate_policy,
        "preview": job.preview,
        "total_rows": job.total_rows,
        "processed_rows": job.processed_rows,
        "successful_rows": job.successful_rows,
        "failed_rows": job.failed_rows,
        "duplicate_rows": job.duplicate_rows,
        "suppressed_rows": job.suppressed_rows,
        "updated_rows": job.updated_rows,
        "error_message": job.error_message,
        "error_report_available": bool(job.error_report_ref and Path(job.error_report_ref).exists()),
        "counts": job.counts,
        "created_at": job.created_at.isoformat() if job.created_at else None,
        "started_at": job.started_at.isoformat() if job.started_at else None,
        "finished_at": job.finished_at.isoformat() if job.finished_at else None,
    }


def _load_job(session: Session, job_id: UUID, tenant_id: UUID) -> ImportJob:
    job = session.scalar(select(ImportJob).where(ImportJob.id == job_id, ImportJob.tenant_id == tenant_id))
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import job not found")
    return job


def _validate_mapping_update(session: Session, tenant_id: UUID, mapping: dict[str, str], headers: list[str]) -> None:
    if not mapping.get("email"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="An email column is required")
    definitions = ContactFieldService(session, tenant_id).definitions_map()
    allowed = STANDARD_FIELDS | {"full_name"} | set(definitions)
    header_set = set(headers)
    for field_name, column in mapping.items():
        if field_name not in allowed and field_name not in definitions:
            if definitions:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Mapping references an unknown field: {field_name}")
        if column not in header_set:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=f"Mapping column {column!r} was not found in the file")


@router.post("/preview")
async def preview_import(file: UploadFile = File(...), principal: TenantPrincipal = Depends(require_permission("contacts.create")), session: Session = Depends(get_db)) -> dict[str, object]:
    content = await file.read(MAX_FILE_BYTES + 1)
    try:
        return ImportService(session, principal.tenant_id).preview(file.filename or "", content)
    except ImportValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None


@router.post("", status_code=status.HTTP_202_ACCEPTED)
async def create_import(file: UploadFile = File(...), principal: TenantPrincipal = Depends(require_permission("contacts.create")), session: Session = Depends(get_db)) -> dict[str, object]:
    content = await file.read(MAX_FILE_BYTES + 1)
    service = ImportService(session, principal.tenant_id)
    try:
        extension = service.validate_file(file.filename or "", content)
    except ImportValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    tenant_dir = UPLOAD_DIR / str(principal.tenant_id)
    tenant_dir.mkdir(parents=True, exist_ok=True)
    source_file = tenant_dir / f"{uuid4().hex}{extension}"
    source_file.write_bytes(content)
    UsageService(session, principal.tenant_id, principal.user_id).record_event(
        EVENT_STORAGE_USED,
        quantity=round(len(content) / (1024 * 1024), 4),
        resource_type="import_file",
        metadata={"filename": file.filename or source_file.name},
    )
    job = ImportJob(tenant_id=principal.tenant_id, created_by_id=principal.user_id, filename=file.filename or source_file.name, file_type=extension[1:], status="UPLOADED", column_mapping={}, counts={}, preview={}, source_file_ref=str(source_file))
    session.add(job)
    session.commit()
    try:
        enqueue_validate(job.id, principal.tenant_id, str(source_file))
    except QueueUnavailable as exc:
        # The upload itself succeeded, so keep the row in UPLOADED: the status
        # poll re-queues it as soon as the broker recovers (see _kick_stuck_job).
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Validation could not be queued. Retry status polling on job {job.id}.",
        ) from exc
    return _job_dto(job)


@router.get("/{job_id}")
def get_import(job_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.read")), session: Session = Depends(get_db)) -> dict[str, object]:
    job = _load_job(session, job_id, principal.tenant_id)
    _kick_stuck_job(job)
    return _job_dto(job)


def _kick_stuck_job(job: ImportJob) -> None:
    """Re-queue a job that no worker ever finished.

    The status read is the only thing a polling client is guaranteed to hit, so
    it doubles as a safety net: an import left in an active state past the stale
    window had its worker interrupted, and re-queueing is safe because
    validation never writes contacts and import replay is idempotent under every
    duplicate policy. The heartbeat is ``updated_at``, which every progress
    batch refreshes.

    A broker outage must not turn this read into an error: the retry simply
    happens on a later poll, so the failure is swallowed deliberately.
    """
    source_file = job.source_file_ref
    if not source_file or not Path(source_file).exists():
        return
    heartbeat = job.updated_at.replace(tzinfo=UTC) if job.updated_at and job.updated_at.tzinfo is None else job.updated_at
    if heartbeat is None or (datetime.now(UTC) - heartbeat).total_seconds() < STUCK_JOB_AFTER_SECONDS:
        return
    try:
        if job.status == "IMPORTING":
            enqueue_import(job.id, job.tenant_id, source_file, job.column_mapping, job.duplicate_policy)
        elif job.status in {"UPLOADED", "VALIDATING"}:
            enqueue_validate(job.id, job.tenant_id, source_file)
    except QueueUnavailable:
        logger.warning("Import job %s is stalled and could not be re-queued", job.id)


@router.put("/{job_id}")
def update_import(job_id: UUID, payload: ImportJobUpdate, principal: TenantPrincipal = Depends(require_permission("contacts.create")), session: Session = Depends(get_db)) -> dict[str, object]:
    job = _load_job(session, job_id, principal.tenant_id)
    if job.status not in {"READY"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Cannot update a job in state {job.status}")
    if payload.duplicate_policy is not None and payload.duplicate_policy not in DEDUP_POLICIES:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Invalid duplicate policy")
    headers = list((job.preview or {}).get("headers", []))
    if payload.column_mapping is not None:
        _validate_mapping_update(session, principal.tenant_id, payload.column_mapping, headers)
        job.column_mapping = payload.column_mapping
    if payload.duplicate_policy is not None:
        job.duplicate_policy = payload.duplicate_policy
    session.commit()
    return _job_dto(job)


@router.post("/{job_id}/start")
def start_import(job_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.create")), session: Session = Depends(get_db)) -> dict[str, object]:
    job = _load_job(session, job_id, principal.tenant_id)
    if job.status != "READY":
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Cannot start a job in state {job.status}")
    if not job.source_file_ref or not Path(job.source_file_ref).exists():
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="The uploaded file is no longer available")
    if not job.column_mapping.get("email"):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="An email column is required")
    job.status = "IMPORTING"
    session.commit()
    try:
        enqueue_import(job.id, principal.tenant_id, job.source_file_ref, job.column_mapping, job.duplicate_policy)
    except QueueUnavailable as exc:
        # Nothing is queued, so leaving the job in IMPORTING would strand it
        # until the stale-job reclaimer fires.  Roll it back to READY so the
        # caller can retry immediately.
        job.status = "READY"
        session.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The import queue is unavailable. Retry in a moment.",
        ) from exc
    return _job_dto(job)


@router.post("/{job_id}/retry")
def retry_import(job_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.create")), session: Session = Depends(get_db)) -> dict[str, object]:
    job = _load_job(session, job_id, principal.tenant_id)
    if job.status not in {"FAILED", "CANCELLED"}:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Cannot retry a job in state {job.status}")
    job.status = "READY"
    job.error_message = None
    job.error_report_ref = None
    job.started_at = None
    job.finished_at = None
    job.processed_rows = 0
    job.successful_rows = 0
    job.failed_rows = 0
    job.duplicate_rows = 0
    job.suppressed_rows = 0
    job.updated_rows = 0
    job.counts = {}
    session.commit()
    return _job_dto(job)


@router.post("/{job_id}/cancel")
def cancel_import(job_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.create")), session: Session = Depends(get_db)) -> dict[str, object]:
    job = _load_job(session, job_id, principal.tenant_id)
    if job.status in TERMINAL_JOB_STATES:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=f"Cannot cancel a job in state {job.status}")
    job.status = "CANCELLED"
    job.finished_at = None
    session.commit()
    return _job_dto(job)


@router.get("/{job_id}/errors")
def download_errors(job_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.read")), session: Session = Depends(get_db)) -> StreamingResponse:
    job = _load_job(session, job_id, principal.tenant_id)
    if not job.error_report_ref or not Path(job.error_report_ref).exists():
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Import error report not found")
    report = json.loads(Path(job.error_report_ref).read_text(encoding="utf-8"))
    output = io.StringIO()
    writer = csv.DictWriter(output, fieldnames=["row", "category", "message"])
    writer.writeheader()
    writer.writerows(report.get("errors", []))
    return StreamingResponse(iter([output.getvalue()]), media_type="text/csv", headers={"Content-Disposition": f'attachment; filename="{job.id}-errors.csv"'})