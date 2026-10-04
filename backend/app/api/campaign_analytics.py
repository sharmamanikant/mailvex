from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.security.permissions import TenantPrincipal, require_permission
from app.services.campaign_analytics import (
    CampaignAnalyticsError,
    CampaignAnalyticsNotFoundError,
    CampaignAnalyticsService,
)

router = APIRouter(prefix="/campaigns", tags=["campaign-analytics"])


def _service(
    session: Session, principal: TenantPrincipal
) -> CampaignAnalyticsService:
    return CampaignAnalyticsService(session, principal.tenant_id)


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, CampaignAnalyticsNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, CampaignAnalyticsError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="Analytics service error")


@router.get("/{campaign_id}/analytics")
def campaign_analytics(
    campaign_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        return _service(session, principal).summary(campaign_id)
    except Exception as exc:
        raise _error(exc) from None


@router.get("/{campaign_id}/analytics/timeline")
def campaign_timeline(
    campaign_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> list[dict[str, object]]:
    try:
        return _service(session, principal).timeline(campaign_id)
    except Exception as exc:
        raise _error(exc) from None


@router.get("/{campaign_id}/analytics/recipients")
def campaign_recipient_activity(
    campaign_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    status: str | None = Query(default=None),
    start_date: str | None = Query(default=None),
    end_date: str | None = Query(default=None),
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        service = _service(session, principal)
        return service.recipient_activity(
            campaign_id,
            page=page,
            page_size=page_size,
            status_filter=status,
            start_date=start_date,
            end_date=end_date,
        )
    except Exception as exc:
        raise _error(exc) from None


@router.get("/{campaign_id}/analytics/export")
def campaign_analytics_export(
    campaign_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> Response:
    try:
        csv_data = _service(session, principal).export_csv(campaign_id)
    except Exception as exc:
        raise _error(exc) from None
    filename = f"campaign-{campaign_id}-results.csv"
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@router.get("/reports/delivery")
def delivery_report(
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    try:
        service = _service(session, principal)
        return {
            "campaign_report": service.campaign_report(),
            "sender_report": service.sender_report(),
        }
    except Exception as exc:
        raise _error(exc) from None
