from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Request
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Campaign, SenderAccount
from app.security.permissions import TenantPrincipal, require_permission
from app.services.compliance_profile import ComplianceProfileService
from app.services.compliance_status import ComplianceStatusService
from app.services.policies import PolicyError, PolicyService

router = APIRouter(prefix="/policies", tags=["policies"])

compliance_router = APIRouter(prefix="/compliance", tags=["compliance"])


class PolicyStatusItem(BaseModel):
    policy_type: str
    title: str
    summary: str
    policy_version: str
    accepted: bool
    accepted_version: str | None = None
    accepted_at: datetime | None = None


class PoliciesStatusResponse(BaseModel):
    required: bool
    documents: list[PolicyStatusItem]


class PolicyDocumentResponse(BaseModel):
    policy_type: str
    policy_version: str
    title: str
    summary: str
    body: str
    published_at: datetime | None = None


class ComplianceProfileResponse(BaseModel):
    compliance_profile: str
    jurisdiction: str
    require_unsubscribe: bool
    require_sender_identity: bool
    require_policy_acceptance: bool
    require_consent_metadata: bool
    require_list_unsubscribe_header: bool
    retention_policy: dict[str, Any]
    safety_thresholds: dict[str, Any]


class ComplianceProfileUpdate(BaseModel):
    compliance_profile: str | None = Field(default=None, max_length=50)
    jurisdiction: str | None = Field(default=None, max_length=100)
    require_unsubscribe: bool | None = None
    require_sender_identity: bool | None = None
    require_policy_acceptance: bool | None = None
    require_consent_metadata: bool | None = None
    require_list_unsubscribe_header: bool | None = None
    retention_policy: dict[str, Any] | None = None
    safety_thresholds: dict[str, Any] | None = None


class SenderReviewReleaseRequest(BaseModel):
    review_note: str | None = Field(default=None, max_length=2000)


class SenderComplianceStatusResponse(BaseModel):
    sender_id: UUID
    email: str
    status: str
    reasons: list[str]
    evaluated_at: datetime | None = None
    paused_at: datetime | None = None
    review_complete: bool = False


class CampaignComplianceStatusResponse(BaseModel):
    campaign_id: UUID
    status: str
    reasons: list[str]
    evaluated_at: datetime | None = None
    level: str


# --------------------------------------------------------------------- #
# Policies
# --------------------------------------------------------------------- #
@router.get("/status", response_model=PoliciesStatusResponse)
def policies_status(
    principal: TenantPrincipal = Depends(require_permission("campaigns.read")),
    session: Session = Depends(get_db),
) -> PoliciesStatusResponse:
    service = PolicyService(session, principal.tenant_id, principal.user_id)
    profile = ComplianceProfileService(session, principal.tenant_id).get()
    items = service.status(principal.user_id)
    return PoliciesStatusResponse(
        required=profile.require_policy_acceptance,
        documents=[
            PolicyStatusItem(
                policy_type=item["policy_type"],
                title=item["title"],
                summary=item["summary"],
                policy_version=item["policy_version"],
                accepted=item["accepted"],
                accepted_version=item["accepted_version"],
                accepted_at=item["accepted_at"],
            )
            for item in items
        ],
    )


@router.get("/documents/{policy_type}", response_model=PolicyDocumentResponse)
def policy_document(
    policy_type: str,
    principal: TenantPrincipal = Depends(require_permission("campaigns.read")),
    session: Session = Depends(get_db),
) -> PolicyDocumentResponse:
    service = PolicyService(session, principal.tenant_id, principal.user_id)
    try:
        document = service.get_document(policy_type)
    except PolicyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PolicyDocumentResponse(
        policy_type=document.policy_type,
        policy_version=document.policy_version,
        title=document.title,
        summary=document.summary,
        body=document.body,
        published_at=document.published_at,
    )


@router.post("/documents/{policy_type}/accept", response_model=PolicyStatusItem)
def accept_policy(
    policy_type: str,
    request: Request,
    principal: TenantPrincipal = Depends(require_permission("campaigns.read")),
    session: Session = Depends(get_db),
) -> PolicyStatusItem:
    service = PolicyService(session, principal.tenant_id, principal.user_id)
    try:
        document = service.current_document(policy_type)
        client_ip = request.client.host if request.client else None
        service.accept(policy_type, document.policy_version, ip_address=client_ip)
    except PolicyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return PolicyStatusItem(
        policy_type=policy_type,
        title=document.title,
        summary=document.summary,
        policy_version=document.policy_version,
        accepted=True,
        accepted_version=document.policy_version,
        accepted_at=datetime.now(),
    )


# --------------------------------------------------------------------- #
# Compliance profile
# --------------------------------------------------------------------- #
@compliance_router.get("/profile", response_model=ComplianceProfileResponse)
def get_compliance_profile(
    principal: TenantPrincipal = Depends(require_permission("settings.manage")),
    session: Session = Depends(get_db),
) -> ComplianceProfileResponse:
    profile = ComplianceProfileService(session, principal.tenant_id).get()
    return ComplianceProfileResponse(
        compliance_profile=profile.compliance_profile,
        jurisdiction=profile.jurisdiction,
        require_unsubscribe=profile.require_unsubscribe,
        require_sender_identity=profile.require_sender_identity,
        require_policy_acceptance=profile.require_policy_acceptance,
        require_consent_metadata=profile.require_consent_metadata,
        require_list_unsubscribe_header=profile.require_list_unsubscribe_header,
        retention_policy=profile.retention_policy or {},
        safety_thresholds=profile.safety_thresholds or {},
    )


@compliance_router.patch("/profile", response_model=ComplianceProfileResponse)
def update_compliance_profile(
    payload: ComplianceProfileUpdate,
    principal: TenantPrincipal = Depends(require_permission("settings.manage")),
    session: Session = Depends(get_db),
) -> ComplianceProfileResponse:
    thresholds = payload.safety_thresholds
    if thresholds is not None:
        unsafe = [
            name
            for name, value in thresholds.items()
            if ComplianceProfileService.unsafe_threshold(name, value)
        ]
        if unsafe:
            raise HTTPException(
                status_code=422,
                detail=f"Thresholds outside the safe configurable range: {', '.join(unsafe)}",
            )
    profile = ComplianceProfileService(session, principal.tenant_id, principal.user_id).update(
        compliance_profile=payload.compliance_profile,
        jurisdiction=payload.jurisdiction,
        require_unsubscribe=payload.require_unsubscribe,
        require_sender_identity=payload.require_sender_identity,
        require_policy_acceptance=payload.require_policy_acceptance,
        require_consent_metadata=payload.require_consent_metadata,
        require_list_unsubscribe_header=payload.require_list_unsubscribe_header,
        retention_policy=payload.retention_policy,
        safety_thresholds=payload.safety_thresholds,
    )
    return ComplianceProfileResponse(
        compliance_profile=profile.compliance_profile,
        jurisdiction=profile.jurisdiction,
        require_unsubscribe=profile.require_unsubscribe,
        require_sender_identity=profile.require_sender_identity,
        require_policy_acceptance=profile.require_policy_acceptance,
        require_consent_metadata=profile.require_consent_metadata,
        require_list_unsubscribe_header=profile.require_list_unsubscribe_header,
        retention_policy=profile.retention_policy or {},
        safety_thresholds=profile.safety_thresholds or {},
    )


# --------------------------------------------------------------------- #
# Compliance status
# --------------------------------------------------------------------- #
@compliance_router.get(
    "/senders/{sender_id}/status",
    response_model=SenderComplianceStatusResponse,
)
def sender_compliance_status(
    sender_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("senders.read")),
    session: Session = Depends(get_db),
) -> SenderComplianceStatusResponse:
    sender = session.scalar(
        select(SenderAccount).where(
            SenderAccount.id == sender_id,
            SenderAccount.tenant_id == principal.tenant_id,
        )
    )
    if sender is None:
        raise HTTPException(status_code=404, detail="Sender not found")
    state, reasons = ComplianceStatusService(session, principal.tenant_id).sender_status(sender)
    session.flush()
    session.commit()
    return SenderComplianceStatusResponse(
        sender_id=sender.id,
        email=sender.email,
        status=state,
        reasons=reasons,
        evaluated_at=sender.compliance_evaluated_at,
        paused_at=sender.paused_at,
        review_complete=sender.compliance_status == "COMPLIANT",
    )


@compliance_router.post("/senders/{sender_id}/release", response_model=SenderComplianceStatusResponse)
def release_sender(
    sender_id: UUID,
    payload: SenderReviewReleaseRequest,
    principal: TenantPrincipal = Depends(require_permission("campaigns.approve")),
    session: Session = Depends(get_db),
) -> SenderComplianceStatusResponse:
    from app.services.safety import SenderSafetyError, SenderSafetyService

    try:
        sender = SenderSafetyService(
            session, principal.tenant_id, principal.user_id
        ).release(sender_id, review_note=payload.review_note)
    except SenderSafetyError as exc:
        raise HTTPException(status_code=404, detail=str(exc)) from exc
    return SenderComplianceStatusResponse(
        sender_id=sender.id,
        email=sender.email,
        status=sender.compliance_status,
        reasons=sender.compliance_reasons or [],
        evaluated_at=sender.compliance_evaluated_at,
        paused_at=sender.paused_at,
        review_complete=True,
    )


@compliance_router.get(
    "/campaigns/{campaign_id}/status",
    response_model=CampaignComplianceStatusResponse,
)
def campaign_compliance_status(
    campaign_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("campaigns.read")),
    session: Session = Depends(get_db),
) -> CampaignComplianceStatusResponse:
    campaign = session.scalar(
        select(Campaign).where(
            Campaign.id == campaign_id,
            Campaign.tenant_id == principal.tenant_id,
        )
    )
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    state, reasons = ComplianceStatusService(
        session, principal.tenant_id, principal.user_id
    ).campaign_status(campaign)
    session.commit()
    return CampaignComplianceStatusResponse(
        campaign_id=campaign.id,
        status=state,
        reasons=reasons,
        evaluated_at=campaign.compliance_evaluated_at,
        level=state,
    )