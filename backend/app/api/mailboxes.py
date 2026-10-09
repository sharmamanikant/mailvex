"""API endpoints for workspace mailbox discovery (Phase 2) and Sender
records (Phase 3).

Phase 3 extended this module with bulk Sender creation from discovered
mailboxes. One Sender is created per eligible mailbox; creation is
idempotent and tenant-scoped.

Routes:
  POST /api/v1/provider-connections/{connection_id}/sync
  GET  /api/v1/provider-connections/{connection_id}/mailboxes
  POST /api/v1/provider-connections/{connection_id}/mailboxes/senders
  GET  /api/v1/mailboxes/{mailbox_id}

All endpoints require the existing ``integrations`` RBAC permissions and
enforce tenant isolation via the authenticated ``TenantPrincipal``. No
provider credentials are ever returned.
"""

from __future__ import annotations

import logging
from typing import cast
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Mailbox, ProviderConnection
from app.schemas import mailboxes as mailbox_schemas
from app.schemas import provider_connections as connection_schemas
from app.schemas import workspace_senders as sender_schemas
from app.security.permissions import TenantPrincipal, require_permission
from app.services.mailboxes import (
    MailboxSyncError,
    MailboxSyncService,
    get_mailbox,
    list_mailboxes,
)
from app.services.workspace_senders import (
    BulkSenderValidationError,
    SenderError,
    WorkspaceSenderService,
)

logger = logging.getLogger("crcrm.api.mailboxes")

provider_router = APIRouter(prefix="/provider-connections", tags=["mailboxes"])
mailboxes_router = APIRouter(prefix="/mailboxes", tags=["mailboxes"])


# ------------------------------------------------------------------ #
# Serialization helpers
# ------------------------------------------------------------------ #
def _respond(mailbox: Mailbox, connection: ProviderConnection | None = None) -> mailbox_schemas.MailboxResponse:
    return mailbox_schemas.MailboxResponse(
        id=mailbox.id,
        provider_connection_id=mailbox.provider_connection_id,
        provider_mailbox_id=mailbox.provider_mailbox_id,
        email=mailbox.email,
        display_name=mailbox.display_name,
        first_name=mailbox.first_name,
        last_name=mailbox.last_name,
        department=mailbox.department,
        job_title=mailbox.job_title,
        user_type=mailbox.user_type,
        provider_status=mailbox.provider_status,
        is_suspended=mailbox.is_suspended,
        is_deleted=mailbox.is_deleted,
        last_discovered_at=mailbox.last_discovered_at,
        created_at=mailbox.created_at,
        updated_at=mailbox.updated_at,
    )


def _respond_connection(connection: ProviderConnection) -> connection_schemas.ProviderConnectionResponse:
    return connection_schemas.ProviderConnectionResponse(
        id=connection.id,
        provider=cast(connection_schemas.Provider, connection.provider),
        connection_type=cast(connection_schemas.ProviderConnectionType, connection.connection_type),
        provider_account_id=connection.provider_account_id,
        workspace_domain=connection.workspace_domain,
        display_name=connection.display_name,
        status=cast(connection_schemas.ProviderConnectionStatus, connection.status),
        scopes=connection.scopes or [],
        credential_configured=connection.credential_reference is not None
        and not connection.credential_reference.startswith("revoked:"),
        credential_expires_at=connection.credential_expires_at,
        connected_by=connection.connected_by,
        last_sync_at=connection.last_sync_at,
        last_sync_status=connection.last_sync_status,
        last_sync_error=connection.last_sync_error,
        last_sync_started_at=connection.last_sync_started_at,
        last_sync_completed_at=connection.last_sync_completed_at,
        last_sync_stats=dict(connection.last_sync_stats or {}),
        created_at=connection.created_at,
        updated_at=connection.updated_at,
    )


def _sync_error(exc: MailboxSyncError) -> HTTPException:
    return HTTPException(status_code=exc.status_code, detail=exc.args[0] if exc.args else "Synchronization failed")


def _sender_service(session: Session, principal: TenantPrincipal) -> WorkspaceSenderService:
    return WorkspaceSenderService(session, principal.tenant_id, principal.user_id)


# ------------------------------------------------------------------ #
# Sync
# ------------------------------------------------------------------ #
@provider_router.post("/{connection_id}/sync", response_model=connection_schemas.ProviderConnectionResponse)
def start_mailbox_sync(
    connection_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
) -> connection_schemas.ProviderConnectionResponse:
    """Start (or enqueue) workspace mailbox synchronization."""
    service = MailboxSyncService(session, principal.tenant_id, principal.user_id)
    try:
        connection = service.start_sync(connection_id, request_context={})
    except MailboxSyncError as exc:
        raise _sync_error(exc) from None
    return _respond_connection(connection)


@provider_router.get(
    "/{connection_id}/mailboxes",
    response_model=mailbox_schemas.MailboxPage,
)
def list_connection_mailboxes(
    connection_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.read")),
    session: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str | None = Query(default=None, max_length=200),
    status_filter: str | None = Query(default=None, max_length=20, alias="status"),
) -> mailbox_schemas.MailboxPage:
    """List discovered mailboxes for a provider connection (tenant-scoped)."""
    try:
        mailboxes, total = list_mailboxes(
            session,
            principal.tenant_id,
            connection_id,
            page=page,
            page_size=page_size,
            search=search,
            status=status_filter,
        )
    except MailboxSyncError as exc:
        raise _sync_error(exc) from None
    return mailbox_schemas.MailboxPage(
        items=[_respond(item) for item in mailboxes],
        page=page,
        page_size=page_size,
        total=total,
    )


# ------------------------------------------------------------------ #
# Bulk sender creation (Phase 3)
# ------------------------------------------------------------------ #
@provider_router.post(
    "/{connection_id}/mailboxes/senders",
    response_model=sender_schemas.SenderBulkCreateResponse,
    status_code=status.HTTP_201_CREATED,
)
def bulk_create_senders(
    connection_id: UUID,
    payload: sender_schemas.SenderBulkCreateRequest,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
) -> sender_schemas.SenderBulkCreateResponse:
    """Idempotently create Sender records from eligible discovered mailboxes.

    Selection is EXPLICIT (a bounded ``mailbox_ids`` list) or ALL_ELIGIBLE
    (every active, non-suspended, non-deleted mailbox of the connection).
    Explicit selections validate that every requested mailbox exists in the
    tenant before any write (400 otherwise). Duplicate mailboxes are merged
    into a single Sender via the ``(tenant_id, mailbox_id)`` unique index.
    """
    try:
        return _sender_service(session, principal).create_from_mailboxes(
            connection_id,
            payload.mailbox_ids,
            selection_mode=payload.selection_mode,
        )
    except BulkSenderValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except SenderError as exc:
        if exc.status_code == status.HTTP_404_NOT_FOUND:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
        raise HTTPException(status_code=exc.status_code, detail=str(exc)) from None


# ------------------------------------------------------------------ #
# Mailbox detail
# ------------------------------------------------------------------ #
@mailboxes_router.get("/{mailbox_id}", response_model=mailbox_schemas.MailboxResponse)
def get_mailbox_detail(
    mailbox_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.read")),
    session: Session = Depends(get_db),
) -> mailbox_schemas.MailboxResponse:
    """Return a single mailbox, enforcing tenant ownership."""
    mailbox = get_mailbox(session, principal.tenant_id, mailbox_id)
    if mailbox is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Mailbox not found")
    return _respond(mailbox)
