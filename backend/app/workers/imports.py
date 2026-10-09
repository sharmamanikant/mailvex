from __future__ import annotations

import json
import logging
from datetime import UTC, datetime, timedelta
from pathlib import Path
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.core.database import SessionLocal
from app.models import ImportJob
from app.services.imports import (
    CHUNK_SIZE,
    MAX_COLUMNS,
    MAX_ROWS,
    ImportService,
    ImportValidationError,
)

PROGRESS_BATCH = 100

logger = logging.getLogger(__name__)

IMPORT_JOB_STATES = {
    "UPLOADED",
    "VALIDATING",
    "READY",
    "IMPORTING",
    "COMPLETED",
    "COMPLETED_WITH_ERRORS",
    "FAILED",
    "CANCELLED",
}
TERMINAL_JOB_STATES = {"COMPLETED", "COMPLETED_WITH_ERRORS", "FAILED", "CANCELLED"}
ACTIVE_JOB_STATES = {"UPLOADED", "VALIDATING", "IMPORTING"}

TASK_VALIDATE = "crcrm.run_import_validation"
TASK_IMPORT = "crcrm.run_contact_import"
TASK_VERIFY = "crcrm.run_verification_job"


class QueueUnavailable(RuntimeError):
    """The task broker could not accept the job.

    Distinct from an import failure: the job is still retryable and no partial
    work was performed, so the API maps this onto ``503`` and leaves the job in
    a state the caller can retry from.
    """


def _encode_arg(arg: object) -> object:
    """Serialise a single dispatch argument for the broker.

    Only UUIDs need converting for the JSON serializer; everything else (the
    column-mapping dict, the duplicate policy) is already wire-safe. Blanket
    ``str()`` corrupted the mapping into its ``repr``, so the worker read it as
    a string and ``ImportService.prepare`` died with ``'str' object has no
    attribute 'get'`` while resolving the email column.
    """
    return str(arg) if isinstance(arg, UUID) else arg


def _dispatch(task_name: str, *args: object) -> str:
    """Hand the job to the existing celery/redis queue.

    Imports are deliberately routed through the broker rather than a local
    thread pool: an in-process executor loses every queued job the moment the
    API container is replaced by a deploy, which is exactly the failure the
    import status signal must not have. The celery app is imported lazily to
    keep this module free of an import cycle.

    Raises :class:`QueueUnavailable` when the broker cannot be reached so the
    API answers ``503`` immediately.  Letting the driver exception propagate (or
    block on the default retry schedule) hung the request thread for minutes
    during a broker outage.
    """
    from app.tasks.scheduler import celery_app

    try:
        result = celery_app.send_task(task_name, args=[_encode_arg(arg) for arg in args])
    except Exception as exc:
        raise QueueUnavailable(f"Could not enqueue {task_name}: {exc}") from exc
    return str(result.id)


def enqueue_validate(job_id: UUID, tenant_id: UUID, source_file: str) -> str:
    return _dispatch(TASK_VALIDATE, job_id, tenant_id, source_file)


def enqueue_import(
    job_id: UUID,
    tenant_id: UUID,
    source_file: str,
    mapping: dict[str, str] | None = None,
    policy: str = "SKIP",
) -> str:
    return _dispatch(TASK_IMPORT, job_id, tenant_id, source_file, mapping or {}, policy)


def enqueue_verification(
    tenant_id: UUID,
    created_after: datetime,
    session_factory: sessionmaker[Session] = SessionLocal,
) -> str | None:
    """Queue a verification pass for exactly the contacts this import created.

    Scoping by ``created_after`` rather than by explicit ids keeps the payload
    bounded: a 100k-row import would otherwise hand the broker a 100k-element
    argument list.
    """
    from app.services.verification_jobs import VerificationJobService

    with session_factory() as session:
        verification = VerificationJobService(session, tenant_id).create(
            None,
            filters={"created_after": created_after.isoformat()},
        )
        task_id = _dispatch(TASK_VERIFY, tenant_id, verification.id)
        verification.celery_task_id = task_id
        session.commit()
        return task_id


def stale_active_jobs(
    session: Session,
    older_than_seconds: int | None = None,
    limit: int = 50,
) -> list[ImportJob]:
    """Active jobs whose worker heartbeat has gone quiet.

    ``process_import`` commits every ``PROGRESS_BATCH`` rows, and
    ``updated_at`` carries ``onupdate=func.now()``, so a live job keeps a fresh
    timestamp. A job stuck in VALIDATING/IMPORTING with an old one was orphaned
    by a worker or container that died, and is safe to re-queue: validation
    never writes contacts, and import re-runs are idempotent under every
    duplicate policy.
    """
    seconds = (
        settings.import_stale_job_seconds if older_than_seconds is None else older_than_seconds
    )
    cutoff = datetime.now(UTC) - timedelta(seconds=seconds)
    statement = (
        select(ImportJob)
        .where(ImportJob.status.in_(sorted(ACTIVE_JOB_STATES)))
        .order_by(ImportJob.updated_at)
        .limit(limit)
    )
    jobs = list(session.scalars(statement).all())
    stale: list[ImportJob] = []
    for job in jobs:
        heartbeat = _as_utc(job.updated_at)
        if heartbeat is not None and heartbeat < cutoff:
            stale.append(job)
    return stale


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo is not None else value.replace(tzinfo=UTC)


def reclaim_stale_imports(session: Session) -> dict[str, int]:
    """Re-queue orphaned imports so a status can never hang forever."""
    requeued = 0
    for job in stale_active_jobs(session):
        if not job.source_file_ref or not Path(job.source_file_ref).exists():
            job.status = "FAILED"
            job.error_message = "The uploaded file is no longer available"
            job.finished_at = datetime.now(UTC)
            session.commit()
            continue
        if job.status == "IMPORTING":
            enqueue_import(
                job.id,
                job.tenant_id,
                job.source_file_ref,
                job.column_mapping,
                job.duplicate_policy,
            )
        else:
            enqueue_validate(job.id, job.tenant_id, job.source_file_ref)
        job.updated_at = datetime.now(UTC)
        requeued += 1
    session.commit()
    return {"requeued": requeued}


def process_validate(job_id: UUID, tenant_id: UUID, source_file: str, session_factory: sessionmaker[Session] = SessionLocal) -> None:
    with session_factory() as session:
        job = session.get(ImportJob, job_id)
        if job is None or job.tenant_id != tenant_id or job.status not in {"UPLOADED", "VALIDATING"}:
            return
        job.status = "VALIDATING"
        session.commit()
        try:
            content = Path(source_file).read_bytes()
            service = ImportService(session, tenant_id)
            preview_data = service.preview(job.filename, content)
            headers = list(preview_data["headers"])
            mapping = dict(preview_data["mapping"])
            row_count = int(preview_data["row_count"])
            if row_count > MAX_ROWS:
                raise ImportValidationError("The import exceeds the 100,000 row limit")
            if len(headers) > MAX_COLUMNS:
                raise ImportValidationError("The file has too many columns")
            if not mapping.get("email"):
                raise ImportValidationError("No email column detected in the file")
            job.total_rows = row_count
            job.column_mapping = mapping
            job.preview = preview_data
            job.error_message = None
            job.status = "READY"
            session.commit()
        except Exception as exc:
            session.rollback()
            job = session.get(ImportJob, job_id)
            if job is not None and job.status != "CANCELLED":
                job.status = "FAILED"
                job.error_message = str(exc)
                job.finished_at = datetime.now(UTC)
                session.commit()


def process_import(job_id: UUID, tenant_id: UUID, source_file: str, mapping: dict[str, str] | None = None, policy: str = "SKIP", session_factory: sessionmaker[Session] = SessionLocal) -> None:
    with session_factory() as session:
        job = session.get(ImportJob, job_id)
        if job is None or job.tenant_id != tenant_id or job.status not in {"READY", "IMPORTING"}:
            return
        if job.status == "READY" or job.started_at is None:
            job.status = "IMPORTING"
            job.started_at = datetime.now(UTC)
        # A re-queued job (worker died, then reclaimed) resumes with its
        # original started_at rather than resetting it, so the elapsed-time
        # signal still reflects the first attempt.
        job.finished_at = None
        job.error_message = None
        session.commit()
        try:
            content = Path(source_file).read_bytes()
            service = ImportService(session, tenant_id)
            headers, rows = service.headers_and_rows(job.filename, content)
            context = service.prepare(headers, rows, mapping or job.column_mapping, policy or job.duplicate_policy)
            total = len(rows)
            for start in range(0, total, CHUNK_SIZE):
                session.refresh(job)
                if job.status == "CANCELLED":
                    break
                chunk = rows[start : start + CHUNK_SIZE]
                for sub_start in range(0, len(chunk), PROGRESS_BATCH):
                    session.refresh(job)
                    if job.status == "CANCELLED":
                        break
                    sub = chunk[sub_start : sub_start + PROGRESS_BATCH]
                    service.enforce_contact_headroom(len(sub))
                    service.process_rows(context, sub, start_index=start + sub_start)
                    job.processed_rows = min(start + sub_start + len(sub), total)
                    job.successful_rows = context.report.imported + context.report.updated
                    job.failed_rows = context.report.invalid
                    job.duplicate_rows = context.report.duplicate
                    job.suppressed_rows = context.report.suppressed
                    job.updated_rows = context.report.updated
                    job.counts = context.report.as_dict()
                    session.commit()
                session.refresh(job)
                if job.status == "CANCELLED":
                    break
            if job.status != "CANCELLED":
                report = context.report.as_dict()
                job.status = "COMPLETED" if not report["errors"] else "COMPLETED_WITH_ERRORS"
                job.successful_rows = report["imported"] + report["updated"]
                job.failed_rows = report["invalid"]
                job.duplicate_rows = report["duplicate"]
                job.suppressed_rows = report["suppressed"]
                job.updated_rows = report["updated"]
                job.counts = report
                job.error_report_ref = source_file + ".report.json"
                Path(job.error_report_ref).write_text(json.dumps(report), encoding="utf-8")
                job.finished_at = datetime.now(UTC)
                session.commit()
                if report["imported"] or report["updated"]:
                    try:
                        enqueue_verification(tenant_id, job.started_at, session_factory)
                    except Exception:
                        # The rows are already committed. Losing the verification
                        # hand-off must not rewrite a successful import as FAILED.
                        session.rollback()
                        logger.exception(
                            "Import %s completed but its verification hand-off failed", job_id
                        )
        except Exception as exc:
            session.rollback()
            job = session.get(ImportJob, job_id)
            if job is not None and job.status != "CANCELLED":
                job.status = "FAILED"
                job.error_message = str(exc)
                job.finished_at = datetime.now(UTC)
                session.commit()