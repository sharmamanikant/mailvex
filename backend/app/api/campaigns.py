from __future__ import annotations

import re
from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Campaign, CampaignSender, Contact, SenderAccount, TemplateVersion
from app.schemas.campaigns import (
    CampaignCreate,
    CampaignDetailResponse,
    CampaignResponse,
    CampaignSenderAddRequest,
    CampaignSenderResponse,
    CampaignSenderUpdateRequest,
    CampaignUpdate,
    CampaignValidationResponse,
    StatusChangeRequest,
    ValidationCheckResponse,
)
from app.security.permissions import TenantPrincipal, require_permission
from app.services.campaigns import (
    CampaignError,
    CampaignNotFoundError,
    CampaignService,
    ValidationResult,
)
from app.services.integrations import (
    IntegrationConflictError,
    IntegrationNotFoundError,
    IntegrationValidationError,
)
from app.services.sender_pool import SenderPoolService

router = APIRouter(prefix="/campaigns", tags=["campaigns"])
VARIABLE_PATTERN = re.compile(r"{{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*}}")


def validation_response(result: ValidationResult) -> CampaignValidationResponse:
    from app.services.compliance_status import normalize_reason

    reasons = normalize_reason(
        [check.code for check in result.checks if check.code]
    )
    return CampaignValidationResponse(
        campaign_id=result.campaign_id,
        level=result.level,
        compliance_status=result.level,
        compliance_reasons=reasons,
        checks=[ValidationCheckResponse(name=check.name, outcome=check.outcome, message=check.message, remediation=check.remediation, code=check.code) for check in result.checks],
    )


def response(campaign: Campaign) -> CampaignResponse:
    return CampaignResponse.model_validate(CampaignService.response_data(campaign))


def detail(campaign: Campaign, session: Session) -> CampaignDetailResponse:
    version = max(campaign.versions, key=lambda item: item.version_number) if campaign.versions else None
    template = session.get(TemplateVersion, campaign.template_version_id) if campaign.template_version_id else None
    recipient_preview: dict[str, str] = {}
    if campaign.recipients:
        contact = session.get(Contact, campaign.recipients[0].contact_id)
        if contact:
            recipient_preview = {key: str(value or '') for key, value in {"first_name": contact.first_name, "last_name": contact.last_name, "company": contact.company, "designation": contact.designation, "location": contact.location, "website": contact.website, "industry": contact.industry, "email": contact.email}.items() if value}
            recipient_preview.update({item.field_key: item.field_value or '' for item in contact.custom_fields})
    preview_values = {"first_name": "Recipient", "last_name": "Name", "company": "Their company", "sender_name": "Your name", "sender_company": "Your company", **recipient_preview}
    def resolve(value: str) -> str:
        return VARIABLE_PATTERN.sub(lambda match: preview_values.get(match.group(1), f"[{match.group(1)}]"), value)
    return CampaignDetailResponse(campaign=response(campaign), recipient_ids=[item.contact_id for item in campaign.recipients], latest_version={"id": str(version.id), "version_number": version.version_number, "snapshot": version.snapshot, "is_immutable": version.is_immutable} if version else None, template_content={"subject_template": resolve(template.subject_template), "html_body": resolve(template.html_body), "text_body": resolve(template.text_body) if template.text_body else None, "variables": template.variable_manifest} if template else None, recipient_preview=recipient_preview)


def service(session: Session, principal: TenantPrincipal) -> CampaignService:
    return CampaignService(session, principal.tenant_id, principal.user_id)


def handle(error: Exception) -> NoReturn:
    if isinstance(error, CampaignNotFoundError):
        raise HTTPException(status_code=404, detail="Campaign not found") from None
    raise HTTPException(status_code=400, detail=str(error)) from None


@router.get("", response_model=list[CampaignResponse])
def list_campaigns(principal: TenantPrincipal = Depends(require_permission("campaigns.read")), session: Session = Depends(get_db)) -> list[CampaignResponse]:
    return [response(item) for item in service(session, principal).list()]


@router.post("", response_model=CampaignDetailResponse, status_code=status.HTTP_201_CREATED)
def create_campaign(payload: CampaignCreate, principal: TenantPrincipal = Depends(require_permission("campaigns.create")), session: Session = Depends(get_db)) -> CampaignDetailResponse:
    try:
        return detail(service(session, principal).create(payload), session)
    except (CampaignError, CampaignNotFoundError) as exc:
        handle(exc)


@router.get("/{campaign_id}", response_model=CampaignDetailResponse)
def get_campaign(campaign_id: UUID, principal: TenantPrincipal = Depends(require_permission("campaigns.read")), session: Session = Depends(get_db)) -> CampaignDetailResponse:
    try:
        return detail(service(session, principal)._campaign(campaign_id), session)
    except (CampaignError, CampaignNotFoundError) as exc:
        handle(exc)


@router.patch("/{campaign_id}", response_model=CampaignDetailResponse)
def update_campaign(campaign_id: UUID, payload: CampaignUpdate, principal: TenantPrincipal = Depends(require_permission("campaigns.update")), session: Session = Depends(get_db)) -> CampaignDetailResponse:
    try:
        return detail(service(session, principal).update(campaign_id, payload), session)
    except (CampaignError, CampaignNotFoundError) as exc:
        handle(exc)


@router.post("/{campaign_id}/status", response_model=CampaignDetailResponse)
def change_campaign_status(campaign_id: UUID, payload: StatusChangeRequest, principal: TenantPrincipal = Depends(require_permission("campaigns.approve")), session: Session = Depends(get_db)) -> CampaignDetailResponse:
    try:
        return detail(service(session, principal).transition(campaign_id, payload.status), session)
    except (CampaignError, CampaignNotFoundError) as exc:
        handle(exc)


@router.post("/{campaign_id}/validate", response_model=CampaignValidationResponse)
def validate_campaign(campaign_id: UUID, principal: TenantPrincipal = Depends(require_permission("campaigns.read")), session: Session = Depends(get_db)) -> CampaignValidationResponse:
    try:
        result = validation_response(service(session, principal).validate(campaign_id))
        session.commit()
        return result
    except (CampaignError, CampaignNotFoundError) as exc:
        handle(exc)


@router.post("/{campaign_id}/approve", response_model=CampaignDetailResponse)
def approve_campaign(campaign_id: UUID, principal: TenantPrincipal = Depends(require_permission("campaigns.approve")), session: Session = Depends(get_db)) -> CampaignDetailResponse:
    try:
        return detail(service(session, principal).approve(campaign_id), session)
    except (CampaignError, CampaignNotFoundError) as exc:
        handle(exc)


@router.post("/{campaign_id}/preview", response_model=CampaignDetailResponse)
def preview_campaign(campaign_id: UUID, principal: TenantPrincipal = Depends(require_permission("campaigns.read")), session: Session = Depends(get_db)) -> CampaignDetailResponse:
    try:
           return detail(service(session, principal)._campaign(campaign_id), session)
    except (CampaignError, CampaignNotFoundError) as exc:
        handle(exc)


@router.post("/{campaign_id}/duplicate", response_model=CampaignDetailResponse, status_code=status.HTTP_201_CREATED)
def duplicate_campaign(campaign_id: UUID, name: str = Query(..., min_length=1, max_length=200), principal: TenantPrincipal = Depends(require_permission("campaigns.create")), session: Session = Depends(get_db)) -> CampaignDetailResponse:
    try:
           return detail(service(session, principal).duplicate(campaign_id, name), session)
    except (CampaignError, CampaignNotFoundError) as exc:
        handle(exc)


def pool_service(session: Session, principal: TenantPrincipal) -> SenderPoolService:
    return SenderPoolService(session, principal.tenant_id, principal.user_id)


def pool_response(session: Session, principal: TenantPrincipal, row: CampaignSender) -> CampaignSenderResponse:
    sender = session.scalar(
        select(SenderAccount).where(
            SenderAccount.id == row.sender_id,
            SenderAccount.tenant_id == principal.tenant_id,
        )
    )
    return CampaignSenderResponse(
        id=row.id,
        campaign_id=row.campaign_id,
        sender_id=row.sender_id,
        email=sender.email if sender else str(row.sender_id),
        provider=sender.provider if sender else "UNKNOWN",
        health_status=sender.health_status if sender else "UNKNOWN",
        daily_limit=row.daily_limit,
        enabled=row.enabled,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


def handle_pool(error: Exception) -> NoReturn:
    if isinstance(error, IntegrationNotFoundError):
        raise HTTPException(status_code=404, detail=str(error)) from None
    if isinstance(error, IntegrationConflictError):
        raise HTTPException(status_code=409, detail=str(error)) from None
    raise HTTPException(status_code=400, detail=str(error)) from None


@router.get("/{campaign_id}/senders", response_model=list[CampaignSenderResponse])
def list_campaign_senders(campaign_id: UUID, principal: TenantPrincipal = Depends(require_permission("campaigns.read")), session: Session = Depends(get_db)) -> list[CampaignSenderResponse]:
    return [pool_response(session, principal, row) for row in pool_service(session, principal).list_pool(campaign_id)]


@router.post("/{campaign_id}/senders", response_model=CampaignSenderResponse, status_code=status.HTTP_201_CREATED)
def add_campaign_sender(campaign_id: UUID, payload: CampaignSenderAddRequest, principal: TenantPrincipal = Depends(require_permission("campaigns.update")), session: Session = Depends(get_db)) -> CampaignSenderResponse:
    try:
        row = pool_service(session, principal).add_sender(campaign_id, payload.sender_id, daily_limit=payload.daily_limit, enabled=payload.enabled)
        return pool_response(session, principal, row)
    except (IntegrationNotFoundError, IntegrationConflictError, IntegrationValidationError) as exc:
        handle_pool(exc)


@router.patch("/{campaign_id}/senders/{sender_id}", response_model=CampaignSenderResponse)
def update_campaign_sender(campaign_id: UUID, sender_id: UUID, payload: CampaignSenderUpdateRequest, principal: TenantPrincipal = Depends(require_permission("campaigns.update")), session: Session = Depends(get_db)) -> CampaignSenderResponse:
    try:
        pool = pool_service(session, principal)
        if payload.daily_limit is not None or payload.daily_limit == 0:
            pool.set_daily_limit(campaign_id, sender_id, payload.daily_limit)
        if payload.enabled is not None:
            pool.set_enabled(campaign_id, sender_id, payload.enabled)
        row = pool.list_pool(campaign_id)
        return pool_response(session, principal, next(item for item in row if item.sender_id == sender_id))
    except (IntegrationNotFoundError, IntegrationValidationError) as exc:
        handle_pool(exc)


@router.delete("/{campaign_id}/senders/{sender_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_campaign_sender(campaign_id: UUID, sender_id: UUID, principal: TenantPrincipal = Depends(require_permission("campaigns.update")), session: Session = Depends(get_db)) -> None:
    try:
        pool_service(session, principal).remove_sender(campaign_id, sender_id)
    except IntegrationNotFoundError as exc:
        handle_pool(exc)
