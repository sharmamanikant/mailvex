from __future__ import annotations

from decimal import Decimal
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.billing import (
    EVENT_TO_METRIC,
    METRIC_LABELS,
    PLANS,
    UsageLimitError,
    UsageService,
)
from app.core.database import get_db
from app.models import UsageEvent
from app.security.permissions import TenantPrincipal, require_permission
from app.services.audit import AuditService


class PlanChangeRequest(BaseModel):
    plan_code: str
    reset_period: bool = False
    status: str | None = None


class RecordEventRequest(BaseModel):
    event_type: str
    quantity: Decimal = Field(default=Decimal(1), gt=0)
    resource_type: str | None = None
    resource_id: UUID | None = None
    metadata: dict[str, Any] | None = None


router = APIRouter(prefix="/usage", tags=["usage"])


def _event_dto(event: UsageEvent) -> dict[str, object]:
    return {
        "id": str(event.id),
        "event_type": event.event_type,
        "period_start": event.period_start.isoformat(),
        "quantity": float(event.quantity),
        "resource_type": event.resource_type,
        "resource_id": str(event.resource_id) if event.resource_id else None,
        "metadata": event.event_metadata or {},
        "created_at": event.created_at.isoformat() if event.created_at else None,
    }


@router.get("/overview")
def usage_overview(principal: TenantPrincipal = Depends(require_permission("billing.read")), session: Session = Depends(get_db)) -> dict[str, object]:
    return UsageService(session, principal.tenant_id, principal.user_id).envelope()


@router.get("/plans")
def plans(principal: TenantPrincipal = Depends(require_permission("billing.read")), session: Session = Depends(get_db)) -> list[dict[str, object]]:
    return [
        {
            "code": code,
            "name": planobj.name,
            "price_usd_mo": planobj.price_usd_mo,
            "features": list(planobj.features),
            "limits": planobj.limits,
        }
        for code, planobj in PLANS.items()
    ]


@router.get("/limits")
def limits(principal: TenantPrincipal = Depends(require_permission("billing.read")), session: Session = Depends(get_db)) -> list[dict[str, object]]:
    service = UsageService(session, principal.tenant_id, principal.user_id)
    return [
        {
            "metric": metric,
            "label": METRIC_LABELS.get(metric, metric),
            "limit": limit.limit,
            "scope": limit.scope,
        }
        for metric, limit in service.effective_limits().items()
    ]


@router.get("/events")
def events(event_type: str | None = Query(default=None), limit: int = Query(default=50, ge=1, le=500), principal: TenantPrincipal = Depends(require_permission("billing.read")), session: Session = Depends(get_db)) -> list[dict[str, object]]:
    service = UsageService(session, principal.tenant_id, principal.user_id)
    try:
        rows = service.recent_events(event_type=event_type, limit=limit)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return [_event_dto(event) for event in rows]


@router.patch("/plan")
def change_plan(payload: PlanChangeRequest, principal: TenantPrincipal = Depends(require_permission("billing.manage")), session: Session = Depends(get_db)) -> dict[str, object]:
    from app.billing.plans import validate_plan_code

    try:
        validate_plan_code(payload.plan_code)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    service = UsageService(session, principal.tenant_id, principal.user_id)
    subscription = service.change_plan(payload.plan_code, reset_period=payload.reset_period, status=payload.status)
    session.commit()
    return {
        "tenant_id": str(subscription.tenant_id),
        "plan_code": subscription.plan_code,
        "status": subscription.status,
        "seats": subscription.seats,
        "custom_limits": subscription.custom_limits or {},
        "period_start": subscription.period_start.isoformat() if subscription.period_start else None,
    }


@router.post("/events", status_code=201)
def record_event(payload: RecordEventRequest, principal: TenantPrincipal = Depends(require_permission("billing.manage")), session: Session = Depends(get_db)) -> dict[str, object]:
    service = UsageService(session, principal.tenant_id, principal.user_id)
    if payload.event_type not in EVENT_TO_METRIC:
        raise HTTPException(status_code=400, detail=f"Unknown event type: {payload.event_type}")
    try:
        event = service.meter(
            payload.event_type,
            quantity=payload.quantity,
            resource_type=payload.resource_type,
            resource_id=payload.resource_id,
            metadata=payload.metadata,
        )
    except UsageLimitError:
        raise
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    session.commit()
    AuditService(session, principal.tenant_id, principal.user_id).record(
        "USAGE_EVENT_RECORDED",
        "usage",
        event.id,
        {"event_type": payload.event_type, "quantity": float(payload.quantity)},
    )
    return _event_dto(event)