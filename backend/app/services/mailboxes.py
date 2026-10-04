"""Workspace mailbox synchronization service (Phase 2).

Handles the full lifecycle of a mailbox sync run:
  1. Validate tenant ownership + provider connection state
  2. Set sync status to SYNCING (idempotent — skip if already in progress)
  3. Invoke provider-specific mailbox discovery
  4. Upsert discovered mailboxes into the ``mailboxes`` table
  5. Mark absent records as DELETED (retained for auditability)
  6. Update sync metadata on the connection
  7. Audit-logged with counts (no secrets in metadata)

Credential refresh is handled internally by the discovery provider; this
service never accesses plaintext tokens.

Tenant isolation is enforced on every query: every WHERE clause carries
``tenant_id``.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.email_providers.connection_base import (
    AUTH_REQUIRED,
    EMAIL_PROVIDER_CONNECTION,
    ProviderConnectionError,
)
from app.email_providers.mailbox_discovery import (
    DiscoveredMailbox,
    DiscoveryResult,
    get_mailbox_discovery_provider,
)
from app.models import Mailbox, ProviderConnection
from app.services.audit import AuditService

logger = logging.getLogger("crcrm.mailboxes")

# ------------------------------------------------------------------ #
# Audit action constants
# ------------------------------------------------------------------ #
MAILBOX_SYNC_STARTED = "MAILBOX_SYNC_STARTED"
MAILBOX_SYNC_COMPLETED = "MAILBOX_SYNC_COMPLETED"
MAILBOX_SYNC_FAILED = "MAILBOX_SYNC_FAILED"
MAILBOX_DISCOVERED = "MAILBOX_DISCOVERED"
MAILBOX_UPDATED = "MAILBOX_UPDATED"
MAILBOX_SUSPENDED = "MAILBOX_SUSPENDED"
MAILBOX_SOFT_DELETED = "MAILBOX_SOFT_DELETED"

# Microsoft 365 (Phase 6) keeps its own audit lineage; Google keeps the
# generic MAILBOX_SYNC_* names so existing consumers stay stable.
MICROSOFT_MAILBOX_SYNC_STARTED = "MICROSOFT_MAILBOX_SYNC_STARTED"
MICROSOFT_MAILBOX_SYNC_COMPLETED = "MICROSOFT_MAILBOX_SYNC_COMPLETED"
MICROSOFT_MAILBOX_SYNC_FAILED = "MICROSOFT_MAILBOX_SYNC_FAILED"

_SYNC_AUDIT_ACTIONS: dict[str, dict[str, str]] = {
    "GOOGLE": {
        "STARTED": MAILBOX_SYNC_STARTED,
        "COMPLETED": MAILBOX_SYNC_COMPLETED,
        "FAILED": MAILBOX_SYNC_FAILED,
    },
    "MICROSOFT": {
        "STARTED": MICROSOFT_MAILBOX_SYNC_STARTED,
        "COMPLETED": MICROSOFT_MAILBOX_SYNC_COMPLETED,
        "FAILED": MICROSOFT_MAILBOX_SYNC_FAILED,
    },
}


def _sync_action(provider: str, lifecycle: str) -> str:
    lifecycle = lifecycle.upper()
    table = _SYNC_AUDIT_ACTIONS.get(provider.upper(), _SYNC_AUDIT_ACTIONS["GOOGLE"])
    return table.get(lifecycle, MAILBOX_SYNC_FAILED)


class MailboxSyncError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


def _now() -> datetime:
    return datetime.now(UTC)


def _safe_error_message(exc: Exception) -> str:
    """Convert an exception to a short, non-sensitive error string."""
    if isinstance(exc, ProviderConnectionError):
        return exc.code or "UNKNOWN"
    name = type(exc).__name__
    if len(name) > 80:
        name = name[:80]
    return name or "UNKNOWN"


class MailboxSyncService:
    """Tenant-scoped mailbox synchronization operations."""

    def __init__(self, session: Session, tenant_id: UUID, actor_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id

    def _get_connection(self, connection_id: UUID) -> ProviderConnection:
        conn = self.session.get(ProviderConnection, connection_id)
        if conn is None or conn.tenant_id != self.tenant_id:
            raise MailboxSyncError("Connection not found", status_code=404)
        return conn

    def _audit(self, action: str, resource_id: UUID | None, metadata: dict[str, Any] | None = None) -> None:
        AuditService(self.session, self.tenant_id, self.actor_id).record(
            action, EMAIL_PROVIDER_CONNECTION, resource_id, metadata
        )

    def _audit_connection(self, action: str, conn: ProviderConnection, metadata: dict[str, Any] | None = None) -> None:
        self._audit(
            action,
            conn.id,
            {
                "provider": conn.provider,
                "workspace_domain": conn.workspace_domain,
                "connection_id": str(conn.id),
                **(metadata or {}),
            },
        )

    # ------------------------------------------------------------------ #
    # API-facing: start sync (enqueue or inline)
    # ------------------------------------------------------------------ #
    def start_sync(self, connection_id: UUID, *, request_context: dict[str, Any] | None = None) -> ProviderConnection:
        """Validate and start a mailbox sync. Returns the updated connection.

        If ``settings.mailbox_sync_inline`` is True, the full sync runs
        synchronously before returning. Otherwise the celery task is
        enqueued and the connection status will show SYNCING until the
        worker picks it up.

        Concurrent attempts are serialized with a row lock
        (``SELECT ... FOR UPDATE``; no-op on SQLite) so two requests cannot
        double-enqueue. If enqueue fails, the connection is rolled back to
        its previous sync state so an immediate retry stays possible.
        """
        conn = self.session.scalar(
            select(ProviderConnection).where(ProviderConnection.id == connection_id).with_for_update()
        )
        if conn is None or conn.tenant_id != self.tenant_id:
            raise MailboxSyncError("Connection not found", status_code=404)

        if conn.status not in ("CONNECTED", "ERROR"):
            raise MailboxSyncError(
                "Provider connection is not in a synchronizable state", status_code=409
            )

        if not conn.credential_reference or conn.credential_reference.startswith("revoked:"):
            raise MailboxSyncError(
                "Provider credentials are not configured; please reconnect", status_code=409
            )

        if conn.last_sync_status == "SYNCING" and conn.last_sync_started_at:
            since = (_now() - conn.last_sync_started_at).total_seconds()
            if since < 900:
                raise MailboxSyncError("Synchronization is already in progress", status_code=409)

        now = _now()
        previous_status = conn.last_sync_status
        previous_started_at = conn.last_sync_started_at
        conn.last_sync_status = "SYNCING"
        conn.last_sync_started_at = now
        conn.last_sync_error = None
        conn.updated_at = now
        self._audit_connection(_sync_action(conn.provider, "STARTED"), conn)
        self.session.commit()

        if settings.mailbox_sync_inline:
            run_provider_mailbox_sync(self.session, self.tenant_id, conn.id, actor_id=self.actor_id)
        else:
            try:
                from app.tasks.scheduler import sync_provider_mailboxes
                sync_provider_mailboxes.delay(str(conn.id), str(conn.tenant_id))
            except Exception:
                logger.exception(
                    "Failed to enqueue sync for connection=%s tenant=%s",
                    conn.id,
                    conn.tenant_id,
                )
                conn.last_sync_status = previous_status
                conn.last_sync_started_at = previous_started_at
                conn.last_sync_error = "QUEUE_UNAVAILABLE"
                conn.updated_at = now
                self._audit_connection(
                    _sync_action(conn.provider, "FAILED"),
                    conn,
                    {"reason": "enqueue_failed", "error": "QUEUE_UNAVAILABLE"},
                )
                self.session.commit()
                raise MailboxSyncError(
                    "Synchronization could not be queued; please try again", status_code=503
                ) from None

        self.session.refresh(conn)
        return conn

    # ------------------------------------------------------------------ #
    # Core sync pipeline (used by inline mode + celery task)
    # ------------------------------------------------------------------ #
    def run_sync(self, connection_id: UUID) -> None:
        """Execute discovery + upsert + delete for a pre-staged SYNCING connection.

        Commits on success and on failure. Never raises for provider errors —
        the connection row carries the FAILED status/error instead.
        """
        conn = self._get_connection(connection_id)
        started = _now()
        discovery: DiscoveryResult | None = None

        try:
            provider = get_mailbox_discovery_provider(conn.provider)
            discovery = provider.discover(
                credential_reference=conn.credential_reference or "",
                credential_version=conn.credential_version,
                workspace_domain=conn.workspace_domain,
            )

            if discovery.rotated_credential_reference:
                conn.credential_reference = discovery.rotated_credential_reference
                conn.credential_version = discovery.rotated_credential_version or conn.credential_version
                conn.credential_expires_at = discovery.rotated_expires_at  # type: ignore[assignment]

            created, updated = self._upsert_mailboxes(conn, discovery.all_mailboxes, started)
            suspended = sum(1 for mb in discovery.all_mailboxes if mb.is_suspended)
            deleted = self._mark_absent_as_deleted(conn, discovery.all_mailboxes)

            conn.last_sync_status = "COMPLETED"
            conn.last_sync_error = None
            conn.last_sync_completed_at = _now()
            conn.last_sync_at = _now()
            conn.updated_at = _now()
            duration_ms = (_now() - started).total_seconds() * 1000
            conn.last_sync_stats = {
                "created": created,
                "updated": updated,
                "suspended": suspended,
                "deleted": deleted,
                "skipped": discovery.total_skipped,
                "errors": 0,
                "duration_ms": round(duration_ms),
                "completed_at": conn.last_sync_completed_at.isoformat(),
            }
            self._audit_connection(
                _sync_action(conn.provider, "COMPLETED"),
                conn,
                {
                    "discovered": discovery.total_discovered,
                    "skipped": discovery.total_skipped,
                    "duration_ms": round(duration_ms),
                },
            )

        except ProviderConnectionError as exc:
            is_permanent = exc.code == AUTH_REQUIRED
            if is_permanent:
                from app.email_providers.credentials import (
                    invalidate_credential_reference,
                )
                conn.credential_reference = invalidate_credential_reference(conn.credential_version)
                conn.status = "REVOKED"
                conn.updated_at = _now()
            conn.last_sync_status = "FAILED"
            conn.last_sync_error = exc.code or _safe_error_message(exc)
            conn.last_sync_completed_at = _now()
            conn.updated_at = _now()
            conn.last_sync_stats = dict(conn.last_sync_stats or {})
            conn.last_sync_stats["errors"] = (conn.last_sync_stats.get("errors") or 0) + 1
            conn.last_sync_stats["completed_at"] = conn.last_sync_completed_at.isoformat()
            self._audit_connection(_sync_action(conn.provider, "FAILED"), conn, {"reason": exc.code or "UNKNOWN"})
            logger.warning(
                "sync failed connection=%s code=%s permanent=%s",
                conn.id,
                exc.code,
                is_permanent,
            )

        except Exception as exc:
            conn.last_sync_status = "FAILED"
            conn.last_sync_error = "SYNC_ERROR"
            conn.last_sync_completed_at = _now()
            conn.updated_at = _now()
            conn.last_sync_stats = dict(conn.last_sync_stats or {})
            conn.last_sync_stats["errors"] = (conn.last_sync_stats.get("errors") or 0) + 1
            conn.last_sync_stats["completed_at"] = conn.last_sync_completed_at.isoformat()
            self._audit_connection(_sync_action(conn.provider, "FAILED"), conn, {"error": _safe_error_message(exc)})
            logger.exception("unexpected sync failure connection=%s", conn.id)

        self.session.commit()

    # ------------------------------------------------------------------ #
    # Upsert / delete internals
    # ------------------------------------------------------------------ #
    def _upsert_mailboxes(
        self, conn: ProviderConnection, mailboxes: list[DiscoveredMailbox], discovered_at: datetime
    ) -> tuple[int, int]:
        created = 0
        updated = 0
        for mb in mailboxes:
            existing = self._find_mailbox(conn.id, mb.provider_mailbox_id)
            if existing is not None:
                self._update_mailbox(existing, mb, discovered_at)
                updated += 1
            else:
                self._create_mailbox(conn, mb, discovered_at)
                created += 1
        return created, updated

    def _find_mailbox(self, connection_id: UUID, provider_mailbox_id: str) -> Mailbox | None:
        return self.session.scalar(
            select(Mailbox).where(
                Mailbox.tenant_id == self.tenant_id,
                Mailbox.provider_connection_id == connection_id,
                Mailbox.provider_mailbox_id == provider_mailbox_id,
            )
        )

    def _update_mailbox(self, mailbox: Mailbox, discovered: DiscoveredMailbox, discovered_at: datetime) -> None:
        changed = False
        now = _now()
        for attr, value in [
            ("email", discovered.email),
            ("display_name", discovered.display_name),
            ("first_name", discovered.first_name),
            ("last_name", discovered.last_name),
            ("department", discovered.department),
            ("job_title", discovered.job_title),
            ("user_type", discovered.user_type),
            ("is_suspended", discovered.is_suspended),
        ]:
            if getattr(mailbox, attr) != value:
                setattr(mailbox, attr, value)
                changed = True

        new_status = self._resolve_status(discovered)
        if mailbox.provider_status != new_status:
            mailbox.provider_status = new_status
            changed = True

        if mailbox.is_deleted:
            mailbox.is_deleted = False
            changed = True

        mailbox.last_discovered_at = discovered_at
        mailbox.updated_at = now
        if changed:
            self._audit_connection(
                MAILBOX_UPDATED,
                mailbox.provider_connection,
                {"mailbox_id": str(mailbox.id), "email": mailbox.email},
            )

    def _create_mailbox(self, conn: ProviderConnection, discovered: DiscoveredMailbox, discovered_at: datetime) -> Mailbox:
        mailbox = Mailbox(
            tenant_id=self.tenant_id,
            provider_connection_id=conn.id,
            provider_mailbox_id=discovered.provider_mailbox_id,
            email=discovered.email,
            display_name=discovered.display_name,
            first_name=discovered.first_name,
            last_name=discovered.last_name,
            department=discovered.department,
            job_title=discovered.job_title,
            user_type=discovered.user_type,
            provider_status=self._resolve_status(discovered),
            is_suspended=discovered.is_suspended,
            is_deleted=discovered.is_deleted,
            last_discovered_at=discovered_at,
        )
        self.session.add(mailbox)
        self._audit_connection(
            MAILBOX_DISCOVERED,
            conn,
            {"mailbox_id": str(mailbox.id), "email": mailbox.email, "user_type": mailbox.user_type},
        )
        return mailbox

    def _mark_absent_as_deleted(self, conn: ProviderConnection, mailboxes: list[DiscoveredMailbox]) -> int:
        seen_ids = {mb.provider_mailbox_id for mb in mailboxes}
        now = _now()
        deleted_count = 0
        stale = list(
            self.session.scalars(
                select(Mailbox).where(
                    Mailbox.tenant_id == self.tenant_id,
                    Mailbox.provider_connection_id == conn.id,
                    Mailbox.is_deleted == False,  # noqa: E712
                )
            ).all()
        )
        for mailbox in stale:
            if mailbox.provider_mailbox_id not in seen_ids:
                mailbox.is_deleted = True
                mailbox.provider_status = "DELETED"
                mailbox.updated_at = now
                deleted_count += 1
                self._audit_connection(
                    MAILBOX_SOFT_DELETED,
                    conn,
                    {"mailbox_id": str(mailbox.id), "email": mailbox.email},
                )
        return deleted_count

    @staticmethod
    def _resolve_status(discovered: DiscoveredMailbox) -> str:
        if discovered.is_deleted:
            return "DELETED"
        if discovered.is_suspended:
            return "SUSPENDED"
        return "ACTIVE"


# ------------------------------------------------------------------ #
# Module-level entry point for the celery task
# ------------------------------------------------------------------ #
def run_provider_mailbox_sync(
    session: Session,
    tenant_id: UUID,
    connection_id: UUID,
    *,
    actor_id: UUID | None = None,
) -> None:
    """Run the sync pipeline for a specific tenant + connection.

    The celery task calls this inside a fresh session. Inline sync mode
    also routes through here. The caller is responsible for committing;
    run_sync already commits its own state transitions.
    """
    service = MailboxSyncService(session, tenant_id, actor_id)
    service.run_sync(connection_id)


# ------------------------------------------------------------------ #
# Read helpers (list + detail)
# ------------------------------------------------------------------ #
def list_mailboxes(
    session: Session,
    tenant_id: UUID,
    connection_id: UUID,
    *,
    page: int = 1,
    page_size: int = 50,
    search: str | None = None,
    status: str | None = None,
) -> tuple[list[Mailbox], int]:
    """Paginated, tenant-scoped mailbox list for a single connection."""
    conn = session.get(ProviderConnection, connection_id)
    if conn is None or conn.tenant_id != tenant_id:
        raise MailboxSyncError("Connection not found", status_code=404)

    page = max(page, 1)
    page_size = min(max(page_size, 1), 200)
    filters = [
        Mailbox.tenant_id == tenant_id,
        Mailbox.provider_connection_id == connection_id,
    ]
    if status:
        status_upper = status.upper()
        if status_upper == "ACTIVE":
            filters.append(Mailbox.provider_status == "ACTIVE")
        elif status_upper in ("SUSPENDED", "DELETED", "UNKNOWN"):
            filters.append(Mailbox.provider_status == status_upper)
    if search:
        escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        like_pattern = f"%{escaped}%"
        filters.append(
            (Mailbox.email.ilike(like_pattern, escape="\\"))
            | (Mailbox.display_name.ilike(like_pattern, escape="\\"))
            | (Mailbox.department.ilike(like_pattern, escape="\\"))
            | (Mailbox.job_title.ilike(like_pattern, escape="\\"))
        )

    total = session.scalar(select(func.count(Mailbox.id)).where(*filters)) or 0
    mailboxes = list(
        session.scalars(
            select(Mailbox)
            .where(*filters)
            .order_by(Mailbox.email)
            .offset((page - 1) * page_size)
            .limit(page_size)
        ).all()
    )
    return mailboxes, total


def get_mailbox(session: Session, tenant_id: UUID, mailbox_id: UUID) -> Mailbox | None:
    mailbox = session.get(Mailbox, mailbox_id)
    if mailbox is None or mailbox.tenant_id != tenant_id:
        return None
    return mailbox