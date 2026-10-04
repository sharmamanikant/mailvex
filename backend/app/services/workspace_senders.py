"""Phase 3 + 4 Sender record service.

Senders are application-level sending identities created from eligible
workspace mailboxes. A Sender always maps to exactly one Mailbox inside one
ProviderConnection and is isolated per tenant via the existing chain
Tenant -> ProviderConnection -> Mailbox.

Lifecycle:
  - creation: idempotent bulk create from EXPLICIT mailbox ids or ALL eligible
    mailboxes of a connection; unique (tenant_id, mailbox_id) prevents
    duplicates (INSERT ... ON CONFLICT protects the resize race window).
  - status: ACTIVE | DISABLED | ERROR | REVOKED | REMOVED
  - sending_enabled (bool) is orthogonal to status — a sender can be ACTIVE
    with sending disabled.
  - effective availability: a central evaluation combines Sender status +
    sending_enabled + Mailbox status + ProviderConnection status (Phase 4).
    ``get_sender_availability()`` is the single entry point the future email
    engine calls before delivery.
  - removal is a soft delete (status = REMOVED; the row and its history are
    kept; the Mailbox row is never touched). Restore re-activates a removed
    sender but never auto-enables sending.
  - connection disconnect/revoke propagates to senders (status = REVOKED,
    sending_enabled = False) — never deletes them.

No credentials ever appear here; they stay on the ProviderConnection.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session, selectinload

from app.models import Mailbox, ProviderConnection, Sender
from app.schemas.workspace_senders import (
    SenderAvailability,
    SenderBulkCreateResponse,
    SenderHealthStatus,
    SenderResponse,
    SenderSelectionMode,
    SenderStatus,
)
from app.services.audit import AuditService

logger = logging.getLogger("crcrm.workspace_senders")

# ------------------------------------------------------------------ #
# Audit action constants
# ------------------------------------------------------------------ #
SENDER_CREATED = "SENDER_CREATED"
SENDER_CREATION_SKIPPED = "SENDER_CREATION_SKIPPED"
SENDER_ENABLED = "SENDER_ENABLED"
SENDER_DISABLED = "SENDER_DISABLED"
SENDER_REVOKED = "SENDER_REVOKED"
SENDER_REMOVED = "SENDER_REMOVED"
SENDER_RESTORED = "SENDER_RESTORED"
SENDER_AVAILABILITY_BLOCKED = "SENDER_AVAILABILITY_BLOCKED"
BULK_SENDER_CREATION_STARTED = "BULK_SENDER_CREATION_STARTED"
BULK_SENDER_CREATION_COMPLETED = "BULK_SENDER_CREATION_COMPLETED"
BULK_SENDER_CREATION_FAILED = "BULK_SENDER_CREATION_FAILED"

SENDER_RESOURCE_TYPE = "sender"

ELIGIBLE_MAILBOX_STATUSES = ("ACTIVE",)
SENDER_ACTIVE_STATUSES = ("ACTIVE", "DISABLED", "ERROR")

# ------------------------------------------------------------------ #
# Availability reasons (Phase 4)
# ------------------------------------------------------------------ #
AVAILABILITY_REASONS = {
    "SENDER_DISABLED",
    "SENDER_REMOVED",
    "SENDER_ERROR",
    "SENDER_REVOKED",
    "PROVIDER_DISCONNECTED",
    "PROVIDER_REVOKED",
    "MAILBOX_SUSPENDED",
    "MAILBOX_DELETED",
    "MAILBOX_UNAVAILABLE",
}

# Allow-listed sort fields -> SQLAlchemy column (injection-safe).
SENDER_SORT_FIELDS: dict[str, Any] = {
    "email": Sender.email,
    "created_at": Sender.created_at,
    "updated_at": Sender.updated_at,
    "status": Sender.status,
    "last_health_check_at": Sender.last_health_check_at,
}


def _availability_reason(
    sender: Sender,
    mailbox: Mailbox | None = None,
    connection: ProviderConnection | None = None,
    *,
    ignore_sending_enabled: bool = False,
) -> str | None:
    """Return the single reason a sender is unavailable, or None.

    Evaluation order is deterministic and lifecycle-first: sender status
    dominates, then the sending flag, then mailbox, then provider connection.

    ``ignore_sending_enabled=True`` is used by the enable path: before a user
    flips the flag ON, ``sending_enabled`` is by definition False, so the
    check must ignore it to surface mailbox/provider reasons instead of a
    misleading SENDER_DISABLED.
    """
    if sender.status == "REMOVED":
        return "SENDER_REMOVED"
    if sender.status == "ERROR":
        return "SENDER_ERROR"
    if sender.status == "REVOKED":
        return "SENDER_REVOKED"
    if sender.status == "DISABLED":
        return "SENDER_DISABLED"
    if not ignore_sending_enabled and not sender.sending_enabled:
        return "SENDER_DISABLED"
    if mailbox is not None:
        if mailbox.is_suspended or mailbox.provider_status == "SUSPENDED":
            return "MAILBOX_SUSPENDED"
        if mailbox.is_deleted or mailbox.provider_status == "DELETED":
            return "MAILBOX_DELETED"
        if mailbox.provider_status != "ACTIVE":
            return "MAILBOX_UNAVAILABLE"
    if connection is not None:
        if connection.status == "REVOKED":
            return "PROVIDER_REVOKED"
        if connection.status != "CONNECTED":
            return "PROVIDER_DISCONNECTED"
    return None


def _availability(
    sender: Sender,
    mailbox: Mailbox | None = None,
    connection: ProviderConnection | None = None,
) -> SenderAvailability:
    reason = _availability_reason(sender, mailbox, connection)
    return SenderAvailability(
        available=reason is None,
        sending_enabled=sender.sending_enabled,
        sender_status=cast(SenderStatus, sender.status),
        mailbox_status=mailbox.provider_status if mailbox is not None else "ACTIVE",
        provider_connection_status=connection.status if connection is not None else "CONNECTED",
        reason=reason,
    )


def _enable_blocked_message(reason: str) -> str:
    """Human-safe message for an enable rejected by availability state."""
    return {
        "SENDER_REMOVED": "A removed sender cannot be enabled.",
        "SENDER_ERROR": "The sender is in an error state and cannot be enabled.",
        "SENDER_REVOKED": "The sender was revoked; reconnect the workspace before enabling it.",
        "SENDER_DISABLED": "Sending can only be enabled for an ACTIVE sender.",
        "MAILBOX_SUSPENDED": "The underlying mailbox is suspended and must be restored before this sender can be enabled.",
        "MAILBOX_DELETED": "The underlying mailbox was deleted and must be restored before this sender can be enabled.",
        "MAILBOX_UNAVAILABLE": "The underlying mailbox is unavailable and must be restored before this sender can be enabled.",
        "PROVIDER_REVOKED": "The provider connection was revoked. Reconnect the workspace before this sender can be enabled.",
        "PROVIDER_DISCONNECTED": "The provider connection must be restored before this sender can be enabled.",
    }.get(reason, "The sender cannot be enabled in its current state.")


class SenderError(Exception):
    def __init__(self, message: str, status_code: int = 400) -> None:
        super().__init__(message)
        self.status_code = status_code


class SenderNotFoundError(SenderError):
    def __init__(self, message: str = "Sender not found") -> None:
        super().__init__(message, status_code=404)


class BulkSenderValidationError(SenderError):
    def __init__(self, message: str, invalid_mailbox_ids: list[UUID]) -> None:
        super().__init__(message, status_code=400)
        self.invalid_mailbox_ids = invalid_mailbox_ids


def _now() -> datetime:
    return datetime.now(UTC)


def _respond(sender: Sender) -> SenderResponse:
    availability = _availability(sender, sender.mailbox, sender.provider_connection)
    return SenderResponse(
        id=sender.id,
        tenant_id=sender.tenant_id,
        mailbox_id=sender.mailbox_id,
        provider_connection_id=sender.provider_connection_id,
        email=sender.email,
        display_name=sender.display_name,
        provider=sender.provider,
        status=cast(SenderStatus, sender.status),
        sending_enabled=sender.sending_enabled,
        health_status=cast(SenderHealthStatus, sender.health_status),
        health_score=sender.health_score,
        last_health_check_at=sender.last_health_check_at,
        availability=availability,
        created_at=sender.created_at,
        updated_at=sender.updated_at,
    )


class WorkspaceSenderService:
    """Tenant-scoped operations over Sender records."""

    def __init__(self, session: Session, tenant_id: UUID, actor_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id

    def _audit(self, action: str, resource_id: UUID | None, metadata: dict[str, Any] | None = None) -> None:
        AuditService(self.session, self.tenant_id, self.actor_id).record(
            action, SENDER_RESOURCE_TYPE, resource_id, metadata
        )

    # ------------------------------------------------------------------ #
    # Bulk creation
    # ------------------------------------------------------------------ #
    def create_from_mailboxes(
        self,
        connection_id: UUID,
        mailbox_ids: list[UUID],
        *,
        selection_mode: str = "EXPLICIT",
    ) -> SenderBulkCreateResponse:
        """Idempotently create Sender records for eligible mailboxes.

        EXPLICIT mode validates every requested mailbox belongs to the given
        connection + tenant (missing ids -> 400 before any write). ALL_ELIGIBLE
        ignores ``mailbox_ids`` and targets every eligible mailbox of the
        connection. Eligible = ACTIVE, not suspended, not deleted.

        Returns a summary; never raises after partial work — the response
        carries created/already_exists/skipped/failed counts.
        """
        conn = self.session.get(ProviderConnection, connection_id)
        if conn is None or conn.tenant_id != self.tenant_id:
            raise SenderError("Provider connection not found", status_code=404)

        requested = list(dict.fromkeys(mailbox_ids)) if mailbox_ids else []
        if selection_mode == "EXPLICIT" and not requested:
            raise SenderError("mailbox_ids is required when selection_mode is EXPLICIT")

        self._audit(
            BULK_SENDER_CREATION_STARTED,
            conn.id,
            {
                "connection_id": str(conn.id),
                "provider": conn.provider,
                "selection_mode": selection_mode,
                "requested": len(requested),
            },
        )

        base_filters = [
            Mailbox.tenant_id == self.tenant_id,
            Mailbox.provider_connection_id == conn.id,
        ]

        if selection_mode == "EXPLICIT":
            existing_ids = set(
                self.session.execute(
                    select(Mailbox.id).where(*base_filters, Mailbox.id.in_(requested))
                ).scalars().all()
            )
            missing = set(requested) - existing_ids
            if missing:
                raise BulkSenderValidationError(
                    "Some requested mailboxes were not found in this tenant/connection",
                    sorted(missing),
                )
            eligible_filters = base_filters + [
                Mailbox.id.in_(requested),
                Mailbox.provider_status.in_(ELIGIBLE_MAILBOX_STATUSES),
                Mailbox.is_suspended == False,  # noqa: E712
                Mailbox.is_deleted == False,  # noqa: E712
            ]
        else:
            eligible_filters = base_filters + [
                Mailbox.provider_status.in_(ELIGIBLE_MAILBOX_STATUSES),
                Mailbox.is_suspended == False,  # noqa: E712
                Mailbox.is_deleted == False,  # noqa: E712
            ]

        eligible = list(self.session.scalars(select(Mailbox).where(*eligible_filters)).all())
        eligible_ids = {mailbox.id for mailbox in eligible}

        skipped_ids: set[UUID] = set()
        if selection_mode == "EXPLICIT":
            skipped_ids = set(requested) - eligible_ids
            if skipped_ids:
                skip_meta = {
                    mailbox.id: mailbox.email
                    for mailbox in self.session.scalars(
                        select(Mailbox).where(
                            Mailbox.tenant_id == self.tenant_id,
                            Mailbox.provider_connection_id == conn.id,
                            Mailbox.id.in_(skipped_ids),
                        )
                    ).all()
                }
                for mailbox_id in skipped_ids:
                    self._audit(
                        SENDER_CREATION_SKIPPED,
                        mailbox_id,
                        {
                            "connection_id": str(conn.id),
                            "email": skip_meta.get(mailbox_id, ""),
                            "reason": "ineligible",
                        },
                    )

        eligible_by_id = {mailbox.id: mailbox for mailbox in eligible}
        existing_mailbox_ids = set(
            self.session.execute(
                select(Sender.mailbox_id).where(
                    Sender.tenant_id == self.tenant_id,
                    Sender.mailbox_id.in_(eligible_ids),
                )
            ).scalars().all()
        )

        new_senders = [
            Sender(
                tenant_id=self.tenant_id,
                mailbox_id=mailbox.id,
                provider_connection_id=conn.id,
                email=mailbox.email,
                display_name=mailbox.display_name,
                provider=conn.provider,
            )
            for mailbox in eligible
            if mailbox.id not in existing_mailbox_ids
        ]

        created_count = 0
        if new_senders:
            self._insert_conflict_do_nothing(new_senders)
            self.session.flush()

            created = list(
                self.session.scalars(
                    select(Sender).where(
                        Sender.tenant_id == self.tenant_id,
                        Sender.mailbox_id.in_(eligible_by_id),
                    )
                ).all()
            )
            created_count = max(0, len(created) - len(existing_mailbox_ids))
            for sender in created:
                self._audit(
                    SENDER_CREATED,
                    sender.id,
                    {
                        "mailbox_id": str(sender.mailbox_id),
                        "connection_id": str(conn.id),
                        "email": sender.email,
                        "provider": sender.provider,
                    },
                )
        else:
            created = list(
                self.session.scalars(
                    select(Sender).where(
                        Sender.tenant_id == self.tenant_id,
                        Sender.mailbox_id.in_(eligible_by_id),
                    )
                ).all()
            )

        already_exists = len(existing_mailbox_ids)
        response = SenderBulkCreateResponse(
            mode="SYNC",
            created=created_count,
            already_exists=already_exists,
            skipped=len(skipped_ids),
            failed=0,
            total_attempted=len(skipped_ids) + len(eligible_ids),
            senders=[_respond(sender) for sender in created],
            selection_mode=cast(SenderSelectionMode, selection_mode),
        )
        self._audit(
            BULK_SENDER_CREATION_COMPLETED,
            conn.id,
            {
                "created": created_count,
                "already_exists": already_exists,
                "skipped": len(skipped_ids),
                "eligible": len(eligible_ids),
                "selection_mode": selection_mode,
            },
        )
        self.session.commit()
        return response

    def _insert_conflict_do_nothing(self, senders: list[Sender]) -> None:
        """Bulk insert with an ON CONFLICT DO NOTHING guard.

        Works for both PostgreSQL and SQLite (tests) against the unique
        (tenant_id, mailbox_id) index, making concurrent same-selection
        requests safe: only one writer creates the row.
        """
        bind = self.session.get_bind()
        dialect = getattr(bind, "dialect", None)
        stmt: Any
        if dialect is not None and getattr(dialect, "name", "") == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            stmt = pg_insert(Sender).values([_sender_values(sender) for sender in senders])
        else:
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            stmt = sqlite_insert(Sender).values([_sender_values(sender) for sender in senders])
        stmt = stmt.on_conflict_do_nothing(
            index_elements=["tenant_id", "mailbox_id"],
        )
        self.session.execute(stmt)

    # ------------------------------------------------------------------ #
    # Read: list + detail
    # ------------------------------------------------------------------ #
    def resolve_eligible_mailbox_ids(self, connection_id: UUID) -> tuple[list[UUID], int]:
        """Return (ids, count) of mailboxes eligible for Sender creation."""
        conn = self.session.get(ProviderConnection, connection_id)
        if conn is None or conn.tenant_id != self.tenant_id:
            raise SenderError("Provider connection not found", status_code=404)
        ids = list(
            self.session.execute(
                select(Mailbox.id).where(
                    Mailbox.tenant_id == self.tenant_id,
                    Mailbox.provider_connection_id == conn.id,
                    Mailbox.provider_status.in_(ELIGIBLE_MAILBOX_STATUSES),
                    Mailbox.is_suspended == False,  # noqa: E712
                    Mailbox.is_deleted == False,  # noqa: E712
                )
            ).scalars().all()
        )
        return ids, len(ids)

    def list_senders(
        self,
        *,
        page: int = 1,
        page_size: int = 50,
        search: str | None = None,
        provider: str | None = None,
        status: str | None = None,
        sending: bool | None = None,
        health_status: str | None = None,
        sort: str | None = None,
    ) -> tuple[list[Sender], int]:
        """Paginated, tenant-scoped sender list with optional filters + sort.

        ``search`` matches sender email, sender display name and the
        underlying mailbox email. ``sort`` must be one of the allow-listed
        ``SENDER_SORT_FIELDS`` keys, optionally prefixed with ``-`` for
        descending (never interpolated into SQL by the caller).
        """
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        filters = [Sender.tenant_id == self.tenant_id]
        if provider:
            filters.append(Sender.provider == provider.upper())
        if status:
            filters.append(Sender.status == status.upper())
        if sending is not None:
            filters.append(Sender.sending_enabled == sending)
        if health_status:
            filters.append(Sender.health_status == health_status.upper())
        if search:
            escaped = search.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
            like_pattern = f"%{escaped}%"
            filters.append(
                (Sender.email.ilike(like_pattern, escape="\\"))
                | (Sender.display_name.ilike(like_pattern, escape="\\"))
                | (Mailbox.email.ilike(like_pattern, escape="\\"))
            )

        query = select(Sender).where(*filters)
        count_query = select(func.count(Sender.id)).where(*filters)
        if search:
            query = query.join(Mailbox, Sender.mailbox_id == Mailbox.id)
            count_query = count_query.join(Mailbox, Sender.mailbox_id == Mailbox.id)

        total = self.session.scalar(count_query) or 0

        order_columns, descending = self._resolve_sort(sort)
        query = query.order_by(
            *[SENDER_SORT_FIELDS[column].desc() if descending else SENDER_SORT_FIELDS[column]
              for column in (order_columns or ["email"])]
        )
        senders = list(
            self.session.scalars(
                query.options(selectinload(Sender.mailbox), selectinload(Sender.provider_connection))
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
        )
        return senders, total or 0

    @staticmethod
    def _resolve_sort(sort: str | None) -> tuple[list[str] | None, bool]:
        """Map a client sort token onto the allow-list.

        Accepted: one of the SENDER_SORT_FIELDS keys, optionally prefixed
        with ``-`` for descending. Anything else falls back to the default
        (email ascending) and is never echoed into SQL directly.
        """
        if not sort:
            return None, False
        descending = False
        token = sort
        if token.startswith("-"):
            descending = True
            token = token[1:]
        if "," in token:
            token = token.split(",")[0]
        if token in SENDER_SORT_FIELDS:
            return [token], descending
        if token == "last_health_check":
            return ["last_health_check_at"], descending
        return None, False

    def get_sender(self, sender_id: UUID) -> Sender:
        sender = self.session.scalars(
            select(Sender)
            .where(Sender.id == sender_id, Sender.tenant_id == self.tenant_id)
            .options(selectinload(Sender.mailbox), selectinload(Sender.provider_connection))
        ).first()
        if sender is None:
            raise SenderNotFoundError()
        return sender

    # ------------------------------------------------------------------ #
    # Phase 4: effective availability + detail
    # ------------------------------------------------------------------ #
    def get_sender_availability(self, sender_id: UUID) -> SenderAvailability:
        """Return the sender's effective availability (tenant-scoped).

        This is the single evaluation entry point. The future email engine
        calls the module-level ``get_sender_availability(session, tenant_id,
        sender_id)`` before delivery.
        """
        sender = self.get_sender(sender_id)
        return _availability(sender, sender.mailbox, sender.provider_connection)

    def get_sender_detail(
        self, sender_id: UUID
    ) -> tuple[Sender, Mailbox | None, ProviderConnection | None, SenderAvailability]:
        """Resolve a sender plus its related mailbox/provider and availability."""
        sender = self.get_sender(sender_id)
        mailbox = sender.mailbox if sender.mailbox is not None else None
        connection = (
            sender.provider_connection if sender.provider_connection is not None else None
        )
        return sender, mailbox, connection, _availability(sender, mailbox, connection)

    # ------------------------------------------------------------------ #
    # Mutations: patch + soft removal + restore
    # ------------------------------------------------------------------ #
    def update_sender(
        self,
        sender_id: UUID,
        *,
        sending_enabled: bool | None = None,
        display_name: str | None = None,
    ) -> Sender:
        sender = self.get_sender(sender_id)
        if sender.status == "REMOVED":
            raise SenderError("A removed sender cannot be modified", status_code=409)

        if sending_enabled is not None:
            if sending_enabled:
                if sender.status != "ACTIVE":
                    raise SenderError(
                        "Sending can only be enabled for an ACTIVE sender "
                        f"(current status: {sender.status})",
                        status_code=409,
                    )
                if not sender.sending_enabled:
                    reason = _availability_reason(
                        sender,
                        sender.mailbox,
                        sender.provider_connection,
                        ignore_sending_enabled=True,
                    )
                    if reason is not None:
                        self._audit(
                            SENDER_AVAILABILITY_BLOCKED,
                            sender.id,
                            {
                                "email": sender.email,
                                "reason": reason,
                                "mailbox_status": (
                                    sender.mailbox.provider_status if sender.mailbox is not None else None
                                ),
                                "provider_status": (
                                    sender.provider_connection.status
                                    if sender.provider_connection is not None
                                    else None
                                ),
                            },
                        )
                        self.session.commit()
                        raise SenderError(_enable_blocked_message(reason), status_code=409)
                    sender.sending_enabled = True
                    sender.updated_at = _now()
                    self._audit(SENDER_ENABLED, sender.id, {"email": sender.email})
            elif sender.sending_enabled:
                sender.sending_enabled = False
                sender.updated_at = _now()
                self._audit(SENDER_DISABLED, sender.id, {"email": sender.email})

        if display_name is not None and display_name != sender.display_name:
            sender.display_name = display_name
            sender.updated_at = _now()

        self.session.commit()
        return self.get_sender(sender_id)

    def remove_sender(self, sender_id: UUID) -> Sender:
        """Soft-remove a sender (status = REMOVED). History is preserved."""
        sender = self.get_sender(sender_id)
        if sender.status != "REMOVED":
            sender.status = "REMOVED"
            sender.sending_enabled = False
            sender.updated_at = _now()
            self._audit(
                SENDER_REMOVED,
                sender.id,
                {"email": sender.email, "mailbox_id": str(sender.mailbox_id)},
            )
            self.session.commit()
        return self.get_sender(sender_id)

    def restore_sender(self, sender_id: UUID) -> Sender:
        """Restore a REMOVED sender to ACTIVE with sending disabled.

        Only allowed when the tenant still owns the underlying Mailbox and
        ProviderConnection and both are usable. Sending is never auto-enabled
        after restoration.
        """
        sender = self.get_sender(sender_id)
        if sender.status != "REMOVED":
            raise SenderError("Only a removed sender can be restored", status_code=409)

        mailbox = sender.mailbox
        connection = sender.provider_connection
        if mailbox is None:
            raise SenderError("The sender has no mailbox relationship; it cannot be restored", status_code=409)
        if mailbox.is_deleted or mailbox.provider_status == "DELETED":
            raise SenderError("The underlying mailbox no longer exists; it cannot be restored", status_code=409)
        if mailbox.is_suspended or mailbox.provider_status == "SUSPENDED":
            raise SenderError("The underlying mailbox is suspended; it cannot be restored", status_code=409)
        if mailbox.provider_status != "ACTIVE":
            raise SenderError("The underlying mailbox is not active; it cannot be restored", status_code=409)
        if connection is None:
            raise SenderError("The sender has no provider connection; it cannot be restored", status_code=409)
        if connection.status == "REVOKED":
            raise SenderError("The provider connection was revoked; reconnect the workspace before restoring", status_code=409)
        if connection.status != "CONNECTED":
            raise SenderError("The provider connection is not connected; reconnect before restoring", status_code=409)

        sender.status = "ACTIVE"
        sender.sending_enabled = False
        sender.updated_at = _now()
        self._audit(SENDER_RESTORED, sender.id, {"email": sender.email})
        self.session.commit()
        return self.get_sender(sender_id)


# ------------------------------------------------------------------ #
# Connection-disconnect propagation
# ------------------------------------------------------------------ #
def revoke_senders_for_connection(
    session: Session,
    tenant_id: UUID,
    connection_id: UUID,
    *,
    actor_id: UUID | None = None,
    reason: str = "connection_disconnected",
) -> int:
    """Transition a connection's senders to REVOKED and disable sending.

    Called when a ProviderConnection is disconnected/revoked (or permanently
    fails auth). Sender rows are retained — only status + sending_enabled
    change. Returns the number of senders revoked.
    """
    service = WorkspaceSenderService(session, tenant_id, actor_id)
    senders = list(
        session.scalars(
            select(Sender).where(
                Sender.tenant_id == tenant_id,
                Sender.provider_connection_id == connection_id,
                Sender.status.in_(SENDER_ACTIVE_STATUSES),
            )
        ).all()
    )
    now = _now()
    for sender in senders:
        sender.status = "REVOKED"
        sender.sending_enabled = False
        sender.updated_at = now
        service._audit(
            SENDER_REVOKED,
            sender.id,
            {"email": sender.email, "mailbox_id": str(sender.mailbox_id), "reason": reason},
        )
    return len(senders)


def get_sender_availability(
    session: Session, tenant_id: UUID, sender_id: UUID
) -> SenderAvailability:
    """Module-level entry point for the future email engine.

    Evaluates tenant ownership + Sender status + sending flag + Mailbox status
    + ProviderConnection status before any delivery. Raises
    :class:`SenderNotFoundError` (404) when the sender does not belong to the
    tenant, so callers never learn whether a foreign sender exists.
    """
    return WorkspaceSenderService(session, tenant_id).get_sender_availability(sender_id)


def _sender_values(sender: Sender) -> dict[str, Any]:
    return {
        "tenant_id": sender.tenant_id,
        "mailbox_id": sender.mailbox_id,
        "provider_connection_id": sender.provider_connection_id,
        "email": sender.email,
        "display_name": sender.display_name,
        "provider": sender.provider,
    }