from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Response
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.security.permissions import TenantPrincipal, require_permission
from app.services.reporting import ReportingError, ReportingService

router = APIRouter(prefix="/reports", tags=["reports"])


def _service(session: Session, principal: TenantPrincipal) -> ReportingService:
    return ReportingService(session, principal.tenant_id)


def _filter_params(
    range: Annotated[str | None, Query(pattern="^(today|7d|30d|90d|custom)?$")] = None,
    start_date: str | None = None,
    end_date: str | None = None,
) -> dict[str, str | None]:
    return {"range": range, "start_date": start_date, "end_date": end_date}


def _handle(error: Exception) -> HTTPException:
    if isinstance(error, ReportingError):
        return HTTPException(status_code=400, detail=str(error))
    return HTTPException(status_code=500, detail="Reporting service error")


@router.get("/dashboard")
def dashboard(
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
    filters: dict[str, str | None] = Depends(_filter_params),
) -> dict[str, object]:
    try:
        return _service(session, principal).dashboard(**filters)
    except Exception as error:
        raise _handle(error) from None


@router.get("/campaigns")
def campaigns(
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
    filters: dict[str, str | None] = Depends(_filter_params),
) -> list[dict[str, object]]:
    try:
        return _service(session, principal).campaign_report(**filters)
    except Exception as error:
        raise _handle(error) from None


@router.get("/senders")
def senders(
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
    filters: dict[str, str | None] = Depends(_filter_params),
) -> list[dict[str, object]]:
    try:
        return _service(session, principal).sender_report(**filters)
    except Exception as error:
        raise _handle(error) from None


@router.get("/contacts")
def contacts(
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
    filters: dict[str, str | None] = Depends(_filter_params),
) -> dict[str, int]:
    try:
        return _service(session, principal).contact_report(**filters)
    except Exception as error:
        raise _handle(error) from None


@router.get("/export")
def export(
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
    report: str = Query(..., pattern="^(campaigns|senders|contacts)$"),
    filters: dict[str, str | None] = Depends(_filter_params),
) -> Response:
    try:
        csv_data = _service(session, principal).export_csv(report, **filters)
    except Exception as error:
        raise _handle(error) from None
    return Response(
        content=csv_data,
        media_type="text/csv; charset=utf-8",
        headers={
            "Content-Disposition": (
                f"attachment; filename={report}-report.csv"
            )
        },
    )
