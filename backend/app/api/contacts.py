from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Contact, VerificationJob
from app.schemas.contacts import (
    BulkActionResult,
    BulkContactAction,
    BulkVerifyRequest,
    ContactCreate,
    ContactListCreate,
    ContactListResponse,
    ContactPage,
    ContactResponse,
    ContactTagCreate,
    ContactTagResponse,
    ContactUpdate,
    SingleVerifyRequest,
    VerificationJobResponse,
)
from app.security.permissions import TenantPrincipal, require_permission
from app.services.contacts import (
    ContactConflictError,
    ContactNotFoundError,
    ContactService,
)
from app.services.verification_jobs import VerificationJobError, VerificationJobService

router = APIRouter(prefix="/contacts", tags=["contacts"])


def serialize_contact(contact: Contact) -> ContactResponse:
    return ContactResponse(
        id=contact.id,
        tenant_id=contact.tenant_id,
        first_name=contact.first_name,
        last_name=contact.last_name,
        email=contact.email,
        phone=contact.phone,
        company=contact.company,
        designation=contact.designation,
        location=contact.location,
        website=contact.website,
        industry=contact.industry,
        source=contact.source,
        source_reference=contact.source_reference,
        notes=contact.notes,
        status=contact.status,
        validation_status=contact.validation_status,
        suppression_status=contact.suppression_status,
        unsubscribe_status="UNSUBSCRIBED" if contact.suppression_status == "UNSUBSCRIBED" else "CLEAR",
        custom_fields={item.field_key: item.field_value or "" for item in contact.custom_fields},
        tags=[item.tag.name for item in contact.tag_memberships if item.tag is not None],
        list_ids=[item.contact_list_id for item in contact.list_memberships],
        email_status=contact.email_status,
        email_type=contact.email_type,
        email_provider=contact.email_provider,
        domain_status=contact.domain_status,
        mx_status=contact.mx_status,
        smtp_status=contact.smtp_status,
        disposable=contact.disposable,
        role_account=contact.role_account,
        catch_all=contact.catch_all,
        phone_status=contact.phone_status,
        phone_type=contact.phone_type,
        company_status=contact.company_status,
        duplicate_status=contact.duplicate_status,
        duplicate_score=contact.duplicate_score,
        verification_score=contact.verification_score,
        risk_level=contact.risk_level,
        verification_status=contact.verification_status,
        last_verified_at=contact.last_verified_at,
        verification_details=contact.verification_details,
        created_at=contact.created_at,
        updated_at=contact.updated_at,
    )


def service(session: Session, principal: TenantPrincipal) -> ContactService:
    return ContactService(session, principal.tenant_id, principal.user_id)


@router.get("", response_model=ContactPage)
def get_contacts(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str | None = Query(None, max_length=200),
    status_filter: str | None = Query(None, alias="status", max_length=30),
    source: str | None = Query(None, max_length=100),
    industry: str | None = Query(None, max_length=150),
    tag: str | None = Query(None, max_length=100),
    list_id: UUID | None = Query(None, alias="list"),
    verification_status: str | None = Query(None, max_length=30),
    risk_level: str | None = Query(None, max_length=20),
    sort: str = Query(
        "created_at",
        pattern="^(created_at|updated_at|email|company|designation|location|status"
        "|verification_status|verification_score|risk_level)$",
    ),
    descending: bool = False,
    principal: TenantPrincipal = Depends(require_permission("contacts.read")),
    session: Session = Depends(get_db),
) -> ContactPage:
    contacts, total = service(session, principal).list_contacts(
        page=page,
        page_size=page_size,
        search=search,
        status=status_filter,
        source=source,
        industry=industry,
        tag=tag,
        list_id=list_id,
        sort=sort,
        descending=descending,
        verification_status=verification_status,
        risk_level=risk_level,
    )
    return ContactPage(
        items=[serialize_contact(item) for item in contacts],
        page=page,
        page_size=page_size,
        total=total,
    )


# ------------------------------------------------------------------ #
# Contact validation engine
# ------------------------------------------------------------------ #
@router.post("/{contact_id}/verify", status_code=status.HTTP_202_ACCEPTED)
def verify_contact(
    contact_id: UUID,
    payload: SingleVerifyRequest | None = None,
    principal: TenantPrincipal = Depends(require_permission("contacts.create")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """Queue a single contact for verification.

    Validation performs DNS lookups and optionally SMTP probes, so it always
    runs on the existing celery worker rather than inside the request.
    """
    contact = service(session, principal).get(contact_id)
    probe_smtp = bool(payload.probe_smtp) if payload is not None else False
    from app.tasks.scheduler import verify_contact as verify_contact_task

    task = verify_contact_task.delay(str(principal.tenant_id), str(contact.id))
    return {
        "contact_id": str(contact.id),
        "job_id": task.id,
        "status": "QUEUED",
        "probe_smtp": probe_smtp,
    }


@router.post("/bulk-verify", status_code=status.HTTP_202_ACCEPTED, response_model=VerificationJobResponse)
def bulk_verify(
    payload: BulkVerifyRequest,
    principal: TenantPrincipal = Depends(require_permission("contacts.create")),
    session: Session = Depends(get_db),
) -> VerificationJob:
    """Queue a bulk verification run and return a job reference.

    Contacts are processed in keyset-paged chunks on the celery worker, so a
    million-contact tenant never materialises in the API process.
    """
    from app.tasks.scheduler import run_verification_job

    filters: dict[str, object] = {}
    if payload.source:
        filters["source"] = payload.source
    if payload.status:
        filters["status"] = payload.status
    if payload.list_id:
        filters["list_id"] = str(payload.list_id)
    try:
        job = VerificationJobService(session, principal.tenant_id).create(
            principal.user_id,
            contact_ids=payload.contact_ids,
            probe_smtp=payload.probe_smtp,
            filters=filters or None,
        )
    except VerificationJobError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    task = run_verification_job.delay(str(principal.tenant_id), str(job.id))
    job.celery_task_id = task.id
    session.commit()
    return job


@router.get("/verification-jobs/{job_id}", response_model=VerificationJobResponse)
def get_verification_job(
    job_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("contacts.read")),
    session: Session = Depends(get_db),
) -> VerificationJob:
    job = session.get(VerificationJob, job_id)
    if job is None or job.tenant_id != principal.tenant_id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Verification job not found")
    return job


@router.post("/verification-jobs/{job_id}/cancel", response_model=VerificationJobResponse)
def cancel_verification_job(
    job_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("contacts.create")),
    session: Session = Depends(get_db),
) -> VerificationJob:
    job = VerificationJobService(session, principal.tenant_id).cancel(job_id)
    if job is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Verification job not found")
    return job


@router.get("/verification-summary")
def verification_summary(
    principal: TenantPrincipal = Depends(require_permission("contacts.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    """Aggregate verdict counts for the tenant, computed in SQL.

    Used by the CRM list header so the operator sees the shape of the whole
    audience without paging through it.
    """
    rows = session.execute(
        select(Contact.verification_status, func.count(Contact.id))
        .where(Contact.tenant_id == principal.tenant_id)
        .group_by(Contact.verification_status)
    ).all()
    by_status = {str(status): int(count) for status, count in rows}
    risk_rows = session.execute(
        select(Contact.risk_level, func.count(Contact.id))
        .where(Contact.tenant_id == principal.tenant_id)
        .group_by(Contact.risk_level)
    ).all()
    return {
        "by_status": by_status,
        "by_risk": {str(level): int(count) for level, count in risk_rows},
        "total": sum(by_status.values()),
    }



@router.post("", response_model=ContactResponse, status_code=status.HTTP_201_CREATED)
def create_contact(payload: ContactCreate, principal: TenantPrincipal = Depends(require_permission("contacts.create")), session: Session = Depends(get_db)) -> ContactResponse:
    try:
        return serialize_contact(service(session, principal).create(payload))
    except ContactConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.get("/duplicates")
def get_duplicates(principal: TenantPrincipal = Depends(require_permission("contacts.read")), session: Session = Depends(get_db)) -> list[list[ContactResponse]]:
    return [[serialize_contact(contact) for contact in group] for group in service(session, principal).duplicate_groups()]


@router.post("/duplicates/merge", response_model=ContactResponse)
def merge_duplicate(source_id: UUID, target_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.update")), session: Session = Depends(get_db)) -> ContactResponse:
    try:
        return serialize_contact(service(session, principal).merge(source_id, target_id))
    except ContactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None


@router.post("/bulk", response_model=BulkActionResult)
def bulk_action(payload: BulkContactAction, principal: TenantPrincipal = Depends(require_permission("contacts.update")), session: Session = Depends(get_db)) -> BulkActionResult:
    try:
        report = service(session, principal).bulk(payload.contact_ids, payload.action, payload.target_id, payload.status)
    except ContactNotFoundError as exc:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    except ContactConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return BulkActionResult(affected=report.affected, skipped=report.skipped, failed=report.failed)


@router.get("/lists", response_model=list[ContactListResponse])
def get_lists(principal: TenantPrincipal = Depends(require_permission("contacts.read")), session: Session = Depends(get_db)) -> list[ContactListResponse]:
    return [ContactListResponse.model_validate(item) for item in service(session, principal).lists()]


@router.post("/lists", response_model=ContactListResponse, status_code=status.HTTP_201_CREATED)
def create_list(payload: ContactListCreate, principal: TenantPrincipal = Depends(require_permission("contacts.create")), session: Session = Depends(get_db)) -> ContactListResponse:
    try:
        return ContactListResponse.model_validate(service(session, principal).create_list(payload.name, payload.description))
    except ContactConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.delete("/lists/{list_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_list(list_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.delete")), session: Session = Depends(get_db)) -> Response:
    try:
        service(session, principal).delete_list(list_id)
    except ContactNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="List not found") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/tags", response_model=list[ContactTagResponse])
def get_tags(principal: TenantPrincipal = Depends(require_permission("contacts.read")), session: Session = Depends(get_db)) -> list[ContactTagResponse]:
    return [ContactTagResponse.model_validate(item) for item in service(session, principal).tags()]


@router.post("/tags", response_model=ContactTagResponse, status_code=status.HTTP_201_CREATED)
def create_tag(payload: ContactTagCreate, principal: TenantPrincipal = Depends(require_permission("contacts.create")), session: Session = Depends(get_db)) -> ContactTagResponse:
    try:
        return ContactTagResponse.model_validate(service(session, principal).create_tag(payload.name))
    except ContactConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.get("/{contact_id}", response_model=ContactResponse)
def get_contact(contact_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.read")), session: Session = Depends(get_db)) -> ContactResponse:
    try:
        return serialize_contact(service(session, principal).get(contact_id))
    except ContactNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found") from None


@router.patch("/{contact_id}", response_model=ContactResponse)
def update_contact(contact_id: UUID, payload: ContactUpdate, principal: TenantPrincipal = Depends(require_permission("contacts.update")), session: Session = Depends(get_db)) -> ContactResponse:
    try:
        return serialize_contact(service(session, principal).update(contact_id, payload))
    except ContactNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found") from None
    except ContactConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None


@router.delete("/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_contact(contact_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.delete")), session: Session = Depends(get_db)) -> Response:
    try:
        service(session, principal).delete(contact_id)
    except ContactNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Contact not found") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)
