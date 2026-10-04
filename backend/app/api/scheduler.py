from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.security.permissions import TenantPrincipal, require_permission
from app.services.audit import AuditService
from app.services.delivery_jobs import (
    DeliveryJobError,
    DeliveryJobNotFoundError,
    DeliveryJobService,
)
from app.tasks.scheduler import deliver_job

router = APIRouter(prefix="/campaigns", tags=["scheduler"])


def _http_error(exc: Exception) -> HTTPException:
    if isinstance(exc, DeliveryJobNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, (DeliveryJobError, LookupError)):
        return HTTPException(status_code=409, detail=str(exc))
    return HTTPException(status_code=500, detail=str(exc))


@router.post("/{campaign_id}/schedule")
def schedule_campaign(campaign_id: UUID, principal: TenantPrincipal = Depends(require_permission("campaigns.update")), session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        service = DeliveryJobService(session, principal.tenant_id)
        jobs = service.materialize_for_campaign(campaign_id)
    except Exception as exc:
        raise _http_error(exc) from None
    return {"campaign_id": str(campaign_id), "status": "SCHEDULED", "scheduled_count": len(jobs)}


@router.post("/{campaign_id}/send-now")
def send_campaign_now(campaign_id: UUID, principal: TenantPrincipal = Depends(require_permission("campaigns.update")), session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        service = DeliveryJobService(session, principal.tenant_id)
        jobs = service.send_now(campaign_id)
    except Exception as exc:
        raise _http_error(exc) from None
    if not jobs:
        raise HTTPException(status_code=409, detail="Campaign has no queued messages to send")
    for job in jobs:
        deliver_job.delay(str(job.tenant_id), str(job.id))
    return {"campaign_id": str(campaign_id), "status": "SENDING", "queued_count": len(jobs)}


@router.post("/{campaign_id}/pause")
def pause_campaign(campaign_id: UUID, principal: TenantPrincipal = Depends(require_permission("campaigns.pause")), session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        campaign = DeliveryJobService(session, principal.tenant_id).pause(campaign_id)
    except Exception as exc:
        raise _http_error(exc) from None
    AuditService(session, principal.tenant_id, principal.user_id).record("CAMPAIGN_PAUSED", "campaign", campaign_id)
    session.commit()
    return {"campaign_id": str(campaign.id), "status": campaign.status}


@router.post("/{campaign_id}/resume")
def resume_campaign(campaign_id: UUID, principal: TenantPrincipal = Depends(require_permission("campaigns.resume")), session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        campaign = DeliveryJobService(session, principal.tenant_id).resume(campaign_id)
    except Exception as exc:
        raise _http_error(exc) from None
    AuditService(session, principal.tenant_id, principal.user_id).record("CAMPAIGN_RESUMED", "campaign", campaign_id)
    session.commit()
    return {"campaign_id": str(campaign.id), "status": campaign.status}


@router.post("/{campaign_id}/cancel")
def cancel_campaign(campaign_id: UUID, principal: TenantPrincipal = Depends(require_permission("campaigns.pause")), session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        cancelled = DeliveryJobService(session, principal.tenant_id).cancel_pending(campaign_id)
    except Exception as exc:
        raise _http_error(exc) from None
    AuditService(session, principal.tenant_id, principal.user_id).record("CAMPAIGN_CANCELLED", "campaign", campaign_id)
    session.commit()
    return {"campaign_id": str(campaign_id), "status": "CANCELLED", "cancelled_count": cancelled}


@router.get("/{campaign_id}/delivery-progress")
def delivery_progress(campaign_id: UUID, principal: TenantPrincipal = Depends(require_permission("campaigns.read")), session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        service = DeliveryJobService(session, principal.tenant_id)
        campaign = service.campaign(campaign_id)
        counts = service.progress(campaign_id)
    except Exception as exc:
        raise _http_error(exc) from None
    total = sum(counts.values())
    return {
        "campaign_id": str(campaign_id),
        "status": campaign.status,
        "counts": counts,
        "total": total,
    }
