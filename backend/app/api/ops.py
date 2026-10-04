from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.observability.alerts import AlertService, serialize_alert
from app.observability.metrics import get_metrics
from app.observability.ops import OpsService
from app.security.permissions import TenantPrincipal, require_super_admin
from app.services.readiness import readiness

router = APIRouter(prefix="/ops", tags=["ops"])

OPS_GUARD = require_super_admin


def _ops_service(session: Session) -> OpsService:
    return OpsService(session, get_metrics())


def _alert_service(session: Session) -> AlertService:
    return AlertService(session, get_metrics())


@router.get("/overview")
def ops_overview(_principal: TenantPrincipal = Depends(OPS_GUARD), session: Session = Depends(get_db)) -> dict[str, Any]:
    return _ops_service(session).overview()


@router.get("/readiness")
def ops_readiness(_principal: TenantPrincipal = Depends(OPS_GUARD)) -> dict[str, Any]:
    checks = readiness()
    return {"ready": all(value == "ready" for value in checks.values()), **checks}


@router.get("/metrics")
def ops_metrics(_principal: TenantPrincipal = Depends(OPS_GUARD)) -> dict[str, Any]:
    return {"metrics": get_metrics().snapshot_all()}


@router.get("/metrics/history")
def ops_metrics_history(
    metric: str | None = None,
    limit: int = 100,
    _principal: TenantPrincipal = Depends(OPS_GUARD),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    if not 1 <= limit <= 500:
        raise HTTPException(status_code=422, detail="limit must be between 1 and 500")
    return {"metric": metric, "samples": _ops_service(session).metrics_history(metric=metric, limit=limit)}


@router.get("/alerts")
def ops_alerts(_principal: TenantPrincipal = Depends(OPS_GUARD), session: Session = Depends(get_db), limit: int = 100) -> dict[str, Any]:
    records = _alert_service(session).recent(limit=limit)
    return {"alerts": [serialize_alert(record) for record in records]}


@router.get("/alerts/open")
def ops_alerts_open(_principal: TenantPrincipal = Depends(OPS_GUARD), session: Session = Depends(get_db)) -> dict[str, Any]:
    records = _alert_service(session).open_alerts(limit=100)
    return {"alerts": [serialize_alert(record) for record in records]}


@router.post("/alerts/evaluate")
def ops_alerts_evaluate(_principal: TenantPrincipal = Depends(OPS_GUARD), session: Session = Depends(get_db)) -> dict[str, Any]:
    service = _alert_service(session)
    results = service.evaluate()
    counts = service.reconcile(results)
    return {
        "results": [
            {
                "rule": result.rule,
                "label": result.label,
                "severity": result.severity,
                "message": result.message,
                "details": result.details,
            }
            for result in results
        ],
        **counts,
    }


@router.post("/alerts/{alert_id}/ack")
def ops_alert_ack(alert_id: UUID, principal: TenantPrincipal = Depends(OPS_GUARD), session: Session = Depends(get_db)) -> dict[str, Any]:
    record = _alert_service(session).ack(alert_id, principal.user_id)
    if record is None:
        raise HTTPException(status_code=404, detail="Alert not found or already resolved")
    return {"alert": serialize_alert(record)}


@router.post("/sample")
def ops_sample(_principal: TenantPrincipal = Depends(OPS_GUARD), session: Session = Depends(get_db)) -> dict[str, Any]:
    values = _ops_service(session).sample()
    return {"values": values, "sampled_at": datetime.now(UTC).isoformat()}