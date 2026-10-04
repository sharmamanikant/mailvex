from __future__ import annotations

from fastapi import APIRouter, Depends
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.security.permissions import TenantPrincipal, require_permission
from app.services.analytics import AnalyticsService

router = APIRouter(prefix="/analytics", tags=["analytics"])


@router.get("/dashboard")
def dashboard(
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    return AnalyticsService(session, principal.tenant_id).dashboard()


@router.get("/campaigns")
def campaigns(
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> list[dict[str, object]]:
    return AnalyticsService(session, principal.tenant_id).campaign_report()


@router.get("/senders")
def senders(
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> list[dict[str, object]]:
    return AnalyticsService(session, principal.tenant_id).sender_report()


@router.get("/domains")
def domains(
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> list[dict[str, object]]:
    return AnalyticsService(session, principal.tenant_id).domain_report()
