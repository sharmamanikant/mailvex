from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Campaign, CampaignRecipient, Contact, EmailAccount
from app.schemas.compliance import ComplianceCheckResponse, ComplianceResultResponse
from app.security.permissions import TenantPrincipal, require_permission
from app.services.compliance import ComplianceService

router = APIRouter(prefix="/compliance", tags=["compliance"])


class ComplianceSummaryItem(BaseModel):
    campaign_id: UUID
    recipient_id: UUID
    passed: int
    warnings: int
    blocked: int
    outcome: str


class ComplianceSummaryResponse(BaseModel):
    campaign_id: UUID
    total_recipients: int
    pass_count: int
    warning_count: int
    blocked_count: int
    detail: list[ComplianceSummaryItem]


@router.get("/campaigns/{campaign_id}/recipients/{recipient_id}", response_model=ComplianceResultResponse)
def check_compliance(
    campaign_id: UUID,
    recipient_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("campaigns.read")),
    session: Session = Depends(get_db),
) -> ComplianceResultResponse:
    campaign = session.scalar(
        select(Campaign).where(Campaign.id == campaign_id, Campaign.tenant_id == principal.tenant_id)
    )
    link = session.scalar(
        select(CampaignRecipient).where(
            CampaignRecipient.id == recipient_id,
            CampaignRecipient.tenant_id == principal.tenant_id,
            CampaignRecipient.campaign_id == campaign_id,
        )
    )
    if campaign is None or link is None:
        raise HTTPException(status_code=404, detail="Campaign recipient not found")
    sender = session.scalar(
        select(EmailAccount).where(EmailAccount.id == campaign.sender_id, EmailAccount.tenant_id == principal.tenant_id)
    )
    contact = session.scalar(
        select(Contact).where(Contact.id == link.contact_id, Contact.tenant_id == principal.tenant_id)
    )
    result = ComplianceService(session, principal.tenant_id, check_source="PREVIEW").evaluate(
        campaign, sender, contact, link
    )
    session.commit()
    return ComplianceResultResponse(
        campaign_id=campaign.id,
        recipient_id=link.id,
        outcome=result.outcome,
        checks=[
            ComplianceCheckResponse(
                name=check.name,
                outcome=check.outcome,
                message=check.message,
                remediation=check.remediation,
            )
            for check in result.checks
        ],
    )


@router.get("/campaigns/{campaign_id}/summary", response_model=ComplianceSummaryResponse)
def compliance_summary(
    campaign_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("campaigns.read")),
    session: Session = Depends(get_db),
) -> ComplianceSummaryResponse:
    campaign = session.scalar(
        select(Campaign).where(Campaign.id == campaign_id, Campaign.tenant_id == principal.tenant_id)
    )
    if campaign is None:
        raise HTTPException(status_code=404, detail="Campaign not found")
    from app.models import ComplianceResult

    rows = session.execute(
        select(
            ComplianceResult.campaign_id,
            ComplianceResult.recipient_id,
            ComplianceResult.result,
            func.count(ComplianceResult.id),
        )
        .where(
            ComplianceResult.tenant_id == principal.tenant_id,
            ComplianceResult.campaign_id == campaign_id,
        )
        .group_by(
            ComplianceResult.campaign_id,
            ComplianceResult.recipient_id,
            ComplianceResult.result,
        )
    ).all()

    by_recipient: dict[UUID, dict[str, int]] = {}
    for row in rows:
        if row.recipient_id is None:
            continue
        bucket = by_recipient.setdefault(row.recipient_id, {"passed": 0, "warnings": 0, "blocked": 0})
        key = "blocked" if row.result == "BLOCK" else "warnings" if row.result == "WARNING" else "passed"
        bucket[key] = int(row[3])

    detail: list[ComplianceSummaryItem] = []
    pass_count = warning_count = blocked_count = 0
    for recipient_id, bucket in by_recipient.items():
        outcome = "BLOCK" if bucket["blocked"] else "WARNING" if bucket["warnings"] else "PASS"
        item = ComplianceSummaryItem(
            campaign_id=campaign_id,
            recipient_id=recipient_id,
            passed=bucket["passed"],
            warnings=bucket["warnings"],
            blocked=bucket["blocked"],
            outcome=outcome,
        )
        detail.append(item)
        if outcome == "BLOCK":
            blocked_count += 1
        elif outcome == "WARNING":
            warning_count += 1
        else:
            pass_count += 1

    order = {"BLOCK": 0, "WARNING": 1, "PASS": 2}
    return ComplianceSummaryResponse(
        campaign_id=campaign_id,
        total_recipients=len(by_recipient),
        pass_count=pass_count,
        warning_count=warning_count,
        blocked_count=blocked_count,
        detail=sorted(detail, key=lambda item: (order[item.outcome], item.recipient_id)),
    )
