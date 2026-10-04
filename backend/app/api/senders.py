"""Phase 3 + 4 Sender management API.

The new Sender entity: an application-level sending identity derived from a
workspace mailbox. ``/api/v1/senders`` manages those records (list, detail,
patch, enable/disable, soft-remove, restore). Bulk creation lives on the
provider-connection router (``POST /api/v1/provider-connections/{id}/mailboxes/senders``)
because a Sender is always created from one of that connection's mailboxes.

Phase 4 adds:
  * effective availability on every response (list + detail)
  * richer detail: mailbox + provider connection summaries, health placeholder
  * health-status filtering, allow-listed server-side sorting
  * restore (re-activation) for REMOVED senders
  * enable validation against mailbox + provider state (never silent)

The previous EmailAccount-based sender center was relocated to
``/email-senders`` (``app/api/email_accounts.py``) so its endpoints, frontend,
and tests are preserved.
"""

from __future__ import annotations

from math import ceil
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.models import Mailbox, ProviderConnection, Sender
from app.schemas.sender_health import (
    SenderHealthCheckOut,
    SenderHealthHistoryOut,
    SenderHealthOverviewOut,
)
from app.schemas.workspace_senders import (
    SenderAvailability,
    SenderDetailResponse,
    SenderHealthInfo,
    SenderHealthStatus,
    SenderMailboxSummary,
    SenderOperationResponse,
    SenderPage,
    SenderPatchRequest,
    SenderProviderConnectionSummary,
    SenderResponse,
    SenderStatus,
)
from app.security.permissions import TenantPrincipal, require_permission
from app.security.rate_limit import RateLimitService
from app.services.sender_health_engine import (
    SenderHealthError,
    SenderHealthService,
    build_health_overview_payload,
)
from app.services.workspace_senders import (
    SenderError,
    SenderNotFoundError,
    WorkspaceSenderService,
    _respond,
)

router = APIRouter(prefix="/senders", tags=["senders"])

# Per-sender manual check limiting. Fail-open outside production so a Redis
# outage degrades to "allow" in development but stays strict in production.
health_rate_limits = RateLimitService(
    settings.redis_url, fail_open=settings.app_env != "production"
)


def _service(session: Session, principal: TenantPrincipal) -> WorkspaceSenderService:
    return WorkspaceSenderService(session, principal.tenant_id, principal.user_id)


def _health_service(session: Session, principal: TenantPrincipal) -> SenderHealthService:
    return SenderHealthService(
        session,
        principal.tenant_id,
        principal.user_id,
        rate_limiter=health_rate_limits,
    )


def _http_from_health_error(exc: SenderHealthError) -> HTTPException:
    detail = {"detail": exc.message, "code": exc.code} if exc.code else exc.message
    return HTTPException(status_code=exc.status_code, detail=detail)


def _respond_detail(
    sender: Sender,
    mailbox: Mailbox | None,
    connection: ProviderConnection | None,
    availability: SenderAvailability,
) -> SenderDetailResponse:
    return SenderDetailResponse(
        id=sender.id,
        tenant_id=sender.tenant_id,
        mailbox_id=sender.mailbox_id,
        provider_connection_id=sender.provider_connection_id,
        email=sender.email,
        display_name=sender.display_name,
        provider=sender.provider,
        status=cast(SenderStatus, sender.status),
        sending_enabled=sender.sending_enabled,
        availability=availability,
        mailbox=SenderMailboxSummary(
            id=mailbox.id,
            email=mailbox.email,
            display_name=mailbox.display_name,
            department=mailbox.department,
            job_title=mailbox.job_title,
            status=mailbox.provider_status,
            is_suspended=mailbox.is_suspended,
            is_deleted=mailbox.is_deleted,
            last_discovered_at=mailbox.last_discovered_at,
        )
        if mailbox is not None
        else None,
        provider_connection=SenderProviderConnectionSummary(
            id=connection.id,
            provider=connection.provider,
            status=connection.status,
            workspace_domain=connection.workspace_domain,
            connection_type=connection.connection_type,
            last_sync_status=connection.last_sync_status,
            last_sync_completed_at=connection.last_sync_completed_at,
        )
        if connection is not None
        else None,
        health=SenderHealthInfo(
            status=cast(SenderHealthStatus, sender.health_status),
            score=sender.health_score,
            last_checked_at=sender.last_health_check_at,
        ),
        created_at=sender.created_at,
        updated_at=sender.updated_at,
    )


@router.get("", response_model=SenderPage)
def list_senders(
    principal: TenantPrincipal = Depends(require_permission("integrations.read")),
    session: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
    search: str | None = Query(default=None, max_length=200),
    provider: str | None = Query(default=None, max_length=30),
    status: str | None = Query(default=None, max_length=30),
    sending: bool | None = Query(default=None),
    health_status: str | None = Query(default=None, max_length=30),
    sort: str | None = Query(default=None, max_length=50),
) -> SenderPage:
    """List tenant senders with optional filters, sorting and pagination."""
    service = _service(session, principal)
    senders, total = service.list_senders(
        page=page,
        page_size=page_size,
        search=search,
        provider=provider,
        status=status,
        sending=sending,
        health_status=health_status,
        sort=sort,
    )
    return SenderPage(
        items=[_respond(sender) for sender in senders],
        page=page,
        page_size=page_size,
        total=total,
        total_pages=ceil(total / page_size) if page_size else 0,
    )


# ------------------------------------------------------------------ #
# Phase 5 sender health
# ------------------------------------------------------------------ #
@router.post(
    "/{sender_id}/health-check",
    response_model=SenderHealthCheckOut,
    status_code=status.HTTP_200_OK,
)
def run_sender_health_check(
    sender_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
) -> SenderHealthCheckOut:
    """Trigger a sender health evaluation and return the full outcome."""
    service = _health_service(session, principal)
    try:
        payload = service.run_health_check(sender_id, triggered_by="MANUAL")
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None
    except SenderHealthError as exc:
        raise _http_from_health_error(exc) from None
    return SenderHealthCheckOut.model_validate(payload)


@router.get("/{sender_id}/health", response_model=SenderHealthOverviewOut)
def get_sender_health(
    sender_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.read")),
    session: Session = Depends(get_db),
) -> SenderHealthOverviewOut:
    """Return the sender's current health overview and its latest check."""
    service = _health_service(session, principal)
    try:
        sender = service.get_sender(sender_id)
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None
    latest = service.latest_health(sender_id)
    return SenderHealthOverviewOut.model_validate(build_health_overview_payload(sender, latest))


@router.get("/{sender_id}/health/history", response_model=SenderHealthHistoryOut)
def list_sender_health_history(
    sender_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.read")),
    session: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(25, ge=1, le=200),
) -> SenderHealthHistoryOut:
    """Return paginated health evaluation history for a sender."""
    service = _health_service(session, principal)
    try:
        service.get_sender(sender_id)
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None
    return SenderHealthHistoryOut.model_validate(service.list_history(sender_id, page=page, page_size=page_size))


@router.get("/{sender_id}", response_model=SenderDetailResponse)
def get_sender(
    sender_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.read")),
    session: Session = Depends(get_db),
) -> SenderDetailResponse:
    """Return a single sender with related mailbox/provider and availability."""
    try:
        sender, mailbox, connection, availability = _service(session, principal).get_sender_detail(sender_id)
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None
    return _respond_detail(sender, mailbox, connection, availability)


@router.patch("/{sender_id}", response_model=SenderResponse)
def patch_sender(
    sender_id: UUID,
    payload: SenderPatchRequest,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
) -> SenderResponse:
    """Update a sender's ``sending_enabled`` and/or ``display_name``."""
    service = _service(session, principal)
    try:
        sender = service.update_sender(
            sender_id,
            sending_enabled=payload.sending_enabled,
            display_name=payload.display_name,
        )
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None
    except SenderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    return _respond(sender)


@router.post("/{sender_id}/enable", response_model=SenderOperationResponse)
def enable_sender(
    sender_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
) -> SenderOperationResponse:
    """Explicitly enable sending for a sender (alias for PATCH).

    Rejected when the mailbox/provider state makes the sender unusable, with
    a code-oriented message (e.g. PROVIDER_DISCONNECTED) and an audit event.
    """
    try:
        sender = _service(session, principal).update_sender(sender_id, sending_enabled=True)
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None
    except SenderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    return SenderOperationResponse(sender_id=sender.id, status=sender.status, message="Sending enabled")


@router.post("/{sender_id}/disable", response_model=SenderOperationResponse)
def disable_sender(
    sender_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
) -> SenderOperationResponse:
    """Explicitly disable sending for a sender (alias for PATCH)."""
    try:
        sender = _service(session, principal).update_sender(sender_id, sending_enabled=False)
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None
    except SenderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    return SenderOperationResponse(sender_id=sender.id, status=sender.status, message="Sending disabled")


@router.post("/{sender_id}/restore", response_model=SenderOperationResponse)
def restore_sender(
    sender_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
) -> SenderOperationResponse:
    """Restore a REMOVED sender to ACTIVE (sending stays disabled)."""
    try:
        sender = _service(session, principal).restore_sender(sender_id)
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None
    except SenderError as exc:
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None
    return SenderOperationResponse(sender_id=sender.id, status=sender.status, message="Sender restored; sending remains disabled")


@router.delete("/{sender_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_sender(
    sender_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
) -> Response:
    """Soft-remove a sender (status = REMOVED). History is preserved."""
    try:
        _service(session, principal).remove_sender(sender_id)
    except SenderNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sender not found") from None
    return Response(status_code=status.HTTP_204_NO_CONTENT)