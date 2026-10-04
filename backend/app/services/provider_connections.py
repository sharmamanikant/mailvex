"""Provider-connection service (Phase 1 provider foundation).

Orchestrates the server-controlled OAuth round-trip, tenant-scoped CRUD and
disconnect/revoke lifecycle for organization-level ``ProviderConnection`` rows.

Security guarantees:

* Every query is tenant-scoped; a ``connection_id`` supplied by a caller is
  always combined with the authenticated tenant (``tenant_id + id``), never
  used alone.
* The tenant and user are recovered from the single-use OAuth state, never
  from the callback query string.
* Raw Google tokens are encrypted into an opaque ``credential_reference`` the
  instant they arrive (Fernet, shared credential store) and are tombstoned on
  disconnect/revoke.
* Audit events (PROVIDER_CONNECTION_STARTED / PROVIDER_CONNECTED /
  PROVIDER_CONNECTION_FAILED / PROVIDER_DISCONNECTED / PROVIDER_REVOKED) never
  carry secrets; optional IP + user-agent context is recorded when supplied.
* Errors are safe, user-facing messages with stable error codes - no secrets,
  stack traces, or provider internals leak through.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import log_event
from app.email_providers import get_connection_provider
from app.email_providers.connection_base import (
    AUTH_REQUIRED,
    EMAIL_PROVIDER_CONNECTION,
    NOT_CONFIGURED,
    STATE_EXPIRED,
    STATE_INVALID,
    ProviderConnectionError,
    ProviderIdentity,
)
from app.email_providers.credentials import (
    decrypt_credential_reference,
    encrypt_credential_reference,
    invalidate_credential_reference,
)
from app.email_providers.google.workspace import (
    GOOGLE_WORKSPACE_SCOPES_REQUIRED,
)
from app.email_providers.microsoft.workspace import (
    MICROSOFT_WORKSPACE_SCOPES_REQUIRED,
)
from app.models import ProviderConnection
from app.services.audit import AuditService
from app.services.oauth_state_store import (
    OAuthStateError,
    OAuthStateRecord,
    OAuthStateStore,
    build_state_store,
)

__all__ = [
    "MICROSOFT_CONNECTED",
    "MICROSOFT_CONNECTION_FAILED",
    "MICROSOFT_CONNECTION_STARTED",
    "MICROSOFT_DISCONNECTED",
    "MICROSOFT_REFRESHED",
    "MICROSOFT_REFRESH_FAILED",
    "MICROSOFT_REVOKED",
    "PROVIDER_CONNECTED",
    "PROVIDER_CONNECTION_FAILED",
    "PROVIDER_CONNECTION_STARTED",
    "PROVIDER_DISCONNECTED",
    "PROVIDER_REFRESHED",
    "PROVIDER_REFRESH_FAILED",
    "PROVIDER_REVOKED",
    "ProviderConnectionNotFound",
    "ProviderConnectionService",
    "refresh_due_provider_connections",
]

PROVIDER_CONNECTION_STARTED = "PROVIDER_CONNECTION_STARTED"
PROVIDER_CONNECTED = "PROVIDER_CONNECTED"
PROVIDER_CONNECTION_FAILED = "PROVIDER_CONNECTION_FAILED"
PROVIDER_DISCONNECTED = "PROVIDER_DISCONNECTED"
PROVIDER_REFRESHED = "PROVIDER_REFRESHED"
PROVIDER_REFRESH_FAILED = "PROVIDER_REFRESH_FAILED"
PROVIDER_REVOKED = "PROVIDER_REVOKED"

GOOGLE_PROVIDER = "GOOGLE"
MICROSOFT_PROVIDER = "MICROSOFT"

# Microsoft 365 connection lifecycle audit events (Phase 6). Google keeps the
# generic PROVIDER_* names so existing audit consumers/tests stay stable.
MICROSOFT_CONNECTION_STARTED = "MICROSOFT_CONNECTION_STARTED"
MICROSOFT_CONNECTED = "MICROSOFT_CONNECTED"
MICROSOFT_CONNECTION_FAILED = "MICROSOFT_CONNECTION_FAILED"
MICROSOFT_DISCONNECTED = "MICROSOFT_DISCONNECTED"
MICROSOFT_REVOKED = "MICROSOFT_REVOKED"
MICROSOFT_REFRESHED = "MICROSOFT_REFRESHED"
MICROSOFT_REFRESH_FAILED = "MICROSOFT_REFRESH_FAILED"

_AUDIT_ACTIONS: dict[str, dict[str, str]] = {
    "GOOGLE": {
        "STARTED": PROVIDER_CONNECTION_STARTED,
        "CONNECTED": PROVIDER_CONNECTED,
        "FAILED": PROVIDER_CONNECTION_FAILED,
        "DISCONNECTED": PROVIDER_DISCONNECTED,
        "REVOKED": PROVIDER_REVOKED,
        "REFRESHED": PROVIDER_REFRESHED,
        "REFRESH_FAILED": PROVIDER_REFRESH_FAILED,
    },
    "MICROSOFT": {
        "STARTED": MICROSOFT_CONNECTION_STARTED,
        "CONNECTED": MICROSOFT_CONNECTED,
        "FAILED": MICROSOFT_CONNECTION_FAILED,
        "DISCONNECTED": MICROSOFT_DISCONNECTED,
        "REVOKED": MICROSOFT_REVOKED,
        "REFRESHED": MICROSOFT_REFRESHED,
        "REFRESH_FAILED": MICROSOFT_REFRESH_FAILED,
    },
}


def _audit_action(provider: str, lifecycle: str) -> str:
    lifecycle = lifecycle.upper()
    actions = _AUDIT_ACTIONS.get(provider.upper(), _AUDIT_ACTIONS[GOOGLE_PROVIDER])
    return actions.get(lifecycle, PROVIDER_CONNECTION_FAILED)


class ProviderConnectionNotFound(LookupError):
    pass


@dataclass(frozen=True)
class ResolvedConnection:
    connection: ProviderConnection
    is_new: bool


class ProviderConnectionService:
    """Tenant-scoped lifecycle for provider connections."""

    def __init__(
        self,
        session: Session,
        tenant_id: UUID,
        state_store: OAuthStateStore | None = None,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self._state_store_override = state_store

    # ------------------------------------------------------------------ #
    # Store plumbing
    # ------------------------------------------------------------------ #
    def _state_store(self) -> OAuthStateStore:
        return self._state_store_override or build_state_store()

    @staticmethod
    def _audit(
        session: Session,
        tenant_id: UUID,
        user_id: UUID | None,
        action: str,
        connection: ProviderConnection,
        metadata: dict[str, Any],
    ) -> None:
        AuditService(session, tenant_id, user_id).record(
            action,
            EMAIL_PROVIDER_CONNECTION,
            connection.id,
            metadata,
        )

    # ------------------------------------------------------------------ #
    # Step 1 - start a provider connection
    # ------------------------------------------------------------------ #
    def start_google(self, user_id: UUID, request_context: dict[str, Any] | None = None) -> str:
        """Create/reuse the CONNECTING placeholder and return the consent URL."""
        return self.start(GOOGLE_PROVIDER, user_id, request_context)

    def start_microsoft(self, user_id: UUID, request_context: dict[str, Any] | None = None) -> str:
        """Start the Microsoft 365 org-level consent flow (Phase 6)."""
        return self.start(MICROSOFT_PROVIDER, user_id, request_context)

    def start(self, provider: str, user_id: UUID, request_context: dict[str, Any] | None = None) -> str:
        """Create/reuse the CONNECTING placeholder and return the consent URL."""
        if provider == MICROSOFT_PROVIDER:
            if not settings.microsoft_client_id or not settings.microsoft_client_secret:
                raise ProviderConnectionError(
                    NOT_CONFIGURED,
                    "Microsoft OAuth is not configured for this workspace.",
                )
        elif provider == GOOGLE_PROVIDER:
            if not settings.google_client_id or not settings.google_client_secret:
                raise ProviderConnectionError(
                    NOT_CONFIGURED,
                    "Google OAuth is not configured for this workspace.",
                )
        provider_adapter = get_connection_provider(provider)
        connection = self._placeholder(provider, user_id)
        self.session.add(connection)
        self.session.flush()
        state = self._state_store().create(
            tenant_id=self.tenant_id,
            user_id=user_id,
            connection_id=connection.id,
        )
        self.session.commit()
        try:
            url = provider_adapter.get_authorization_url(state=state)
        except ProviderConnectionError as exc:
            connection.status = "ERROR"
            self.session.commit()
            self._audit_metadata(connection, None, user_id, request_context, reason=exc.code or "ERROR")
            self.session.commit()
            raise
        self._audit_metadata(connection, _audit_action(provider, "STARTED"), user_id, request_context)
        self.session.commit()
        return url

    @staticmethod
    def _required_scopes(provider: str) -> frozenset[str]:
        if provider == MICROSOFT_PROVIDER:
            return MICROSOFT_WORKSPACE_SCOPES_REQUIRED
        return GOOGLE_WORKSPACE_SCOPES_REQUIRED

    def _placeholder(self, provider: str, user_id: UUID) -> ProviderConnection:
        """Reuse an abandoned CONNECTING stub or create a fresh one."""
        existing = self.session.scalar(
            select(ProviderConnection)
            .where(
                ProviderConnection.tenant_id == self.tenant_id,
                ProviderConnection.provider == provider,
                ProviderConnection.status == "CONNECTING",
            )
            .order_by(ProviderConnection.created_at.desc())
        )
        if existing is not None:
            return existing
        return ProviderConnection(
            tenant_id=self.tenant_id,
            provider=provider,
            connection_type="OAUTH",
            status="CONNECTING",
            scopes=[],
            connected_by=user_id,
        )

    # ------------------------------------------------------------------ #
    # Step 2 - callback: exchange, validate, persist
    # ------------------------------------------------------------------ #
    def complete_google(
        self,
        code: str,
        state: str,
        request_context: dict[str, Any] | None = None,
    ) -> ProviderConnection:
        return self.complete(GOOGLE_PROVIDER, code, state, request_context)

    def complete_microsoft(
        self,
        code: str,
        state: str,
        request_context: dict[str, Any] | None = None,
    ) -> ProviderConnection:
        return self.complete(MICROSOFT_PROVIDER, code, state, request_context)

    def complete(
        self,
        provider: str,
        code: str,
        state: str,
        request_context: dict[str, Any] | None = None,
    ) -> ProviderConnection:
        """Consume the state, then run the callback exchange. Tenant and user
        come from the OAuth state, never from the callback query string."""
        try:
            record = self._state_store().consume(state)
        except OAuthStateError as exc:
            raise ProviderConnectionError(
                STATE_EXPIRED if "expired" in str(exc) else STATE_INVALID,
                "This authorization request has expired or was already used. Please start again.",
            ) from exc
        if record.tenant_id != self.tenant_id:
            raise ProviderConnectionError(
                STATE_INVALID,
                "This authorization request is not valid for your workspace.",
            )
        return self.complete_with_state(record, code, state, request_context, provider=provider)

    def complete_with_state(
        self,
        record: OAuthStateRecord,
        code: str,
        state: str,
        request_context: dict[str, Any] | None = None,
        *,
        provider: str = GOOGLE_PROVIDER,
    ) -> ProviderConnection:
        """Callback exchange against an already-consumed state record.

        Used by the browser-callback router, which must resolve the tenant
        from state before it can instantiate a tenant-scoped service.
        ``self.tenant_id`` must match ``record.tenant_id``.
        """
        if record.tenant_id != self.tenant_id:
            raise ProviderConnectionError(
                STATE_INVALID,
                "This authorization request is not valid for your workspace.",
            )
        actor = record.user_id
        adapter = get_connection_provider(provider)
        try:
            result = adapter.handle_callback(
                code=code,
                state=state,
                expected_scopes=self._required_scopes(provider),
            )
        except ProviderConnectionError as exc:
            self._audit_failed(record, exc.code or "OAUTH_EXCHANGE_FAILED", actor, request_context, provider=provider)
            raise
        resolved = self._resolve_for_identity(record, result.identity)
        connection = resolved.connection
        connection.credential_reference = encrypt_credential_reference(
            settings.encryption_key,
            result.credential_payload,
            version="v1",
        )
        connection.credential_version = "v1"
        connection.credential_expires_at = _parse_expiry(result.credential_payload.get("expires_at"))
        self._apply_identity(connection, result.identity, actor, result.connection_metadata)
        self.session.add(connection)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            self._audit_failed(record, "DUPLICATE", actor, request_context, provider=provider)
            raise ProviderConnectionError(
                "DUPLICATE",
                f"A {_provider_label(provider)} connection for this tenant already exists; reconnect to refresh it.",
            ) from None
        self.session.commit()
        self._audit_metadata(
            connection,
            _audit_action(connection.provider, "CONNECTED"),
            actor,
            request_context,
            is_new=resolved.is_new,
        )
        self.session.commit()
        return self.get(connection.id)

    def _resolve_for_identity(
        self,
        record: OAuthStateRecord,
        identity: ProviderIdentity,
    ) -> ResolvedConnection:
        """Find-or-create the connection for (tenant, provider, account).

        A CONNECTING stub that never completed is consolidated onto the
        existing CONNECTED record so duplicate connections can never survive.
        The state-bound placeholder has no credentials or sender data, so
        removing it cannot destroy future Mailbox/Sender records.
        """
        existing = self.session.scalar(
            select(ProviderConnection).where(
                ProviderConnection.tenant_id == record.tenant_id,
                ProviderConnection.provider == identity.provider,
                ProviderConnection.provider_account_id == identity.provider_account_id,
            )
        )
        if record.connection_id is not None:
            placeholder = self.session.scalar(
                select(ProviderConnection).where(
                    ProviderConnection.tenant_id == record.tenant_id,
                    ProviderConnection.id == record.connection_id,
                )
            )
            if placeholder is None:
                raise ProviderConnectionError(
                    STATE_INVALID,
                    "This connection is no longer available. Please start again.",
                )
            if existing is not None and existing.id != placeholder.id:
                if not placeholder.credential_reference:
                    self.session.delete(placeholder)
                return ResolvedConnection(existing, is_new=False)
            return ResolvedConnection(placeholder, is_new=True)
        if existing is not None:
            return ResolvedConnection(existing, is_new=False)
        return ResolvedConnection(
            ProviderConnection(
                tenant_id=record.tenant_id,
                provider=identity.provider,
                connection_type="OAUTH",
                status="CONNECTING",
                scopes=[],
                connected_by=record.user_id,
            ),
            is_new=True,
        )

    def _apply_identity(
        self,
        connection: ProviderConnection,
        identity: ProviderIdentity,
        actor: UUID,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        connection.provider_account_id = identity.provider_account_id
        connection.workspace_domain = identity.workspace_domain
        connection.display_name = identity.display_name
        connection.scopes = sorted(identity.scopes)
        if metadata:
            merged = dict(connection.connection_metadata or {})
            merged.update(metadata)
            connection.connection_metadata = merged
        connection.status = "CONNECTED"
        connection.connected_by = connection.connected_by or actor

    # ------------------------------------------------------------------ #
    # Read / disconnect / revoke (all tenant-scoped)
    # ------------------------------------------------------------------ #
    def list_connections(self) -> list[ProviderConnection]:
        return list(
            self.session.scalars(
                select(ProviderConnection)
                .where(ProviderConnection.tenant_id == self.tenant_id)
                .order_by(ProviderConnection.created_at.desc())
            )
        )

    def get(self, connection_id: UUID) -> ProviderConnection:
        connection = self.session.scalar(
            select(ProviderConnection).where(
                ProviderConnection.tenant_id == self.tenant_id,
                ProviderConnection.id == connection_id,
            )
        )
        if connection is None:
            raise ProviderConnectionNotFound("Provider connection not found")
        return connection

    def disconnect(
        self,
        connection_id: UUID,
        user_id: UUID,
        request_context: dict[str, Any] | None = None,
    ) -> ProviderConnection:
        connection = self.get(connection_id)
        self._revoke_remote_token(connection)
        connection.credential_reference = invalidate_credential_reference(
            connection.credential_version
        )
        connection.status = "DISCONNECTED"
        connection.updated_at = datetime.now(UTC)
        self._audit_metadata(
            connection, _audit_action(connection.provider, "DISCONNECTED"), user_id, request_context
        )
        self.session.commit()
        return self.get(connection_id)

    def revoke(
        self,
        connection_id: UUID,
        user_id: UUID,
        request_context: dict[str, Any] | None = None,
    ) -> ProviderConnection:
        """Mark a connection REVOKED (provider confirmed bad credentials).

        Kept distinct from a user-initiated disconnect so the audit trail can
        distinguish provider-side revocation from an explicit opt-out.
        """
        connection = self.get(connection_id)
        connection.credential_reference = invalidate_credential_reference(
            connection.credential_version
        )
        connection.status = "REVOKED"
        connection.updated_at = datetime.now(UTC)
        self._audit_metadata(
            connection, _audit_action(connection.provider, "REVOKED"), user_id, request_context
        )
        self.session.commit()
        return self.get(connection_id)

    def _revoke_remote_token(self, connection: ProviderConnection) -> None:
        """Best-effort remote revoke; secrets never leave the server boundary."""
        if not connection.credential_reference:
            return
        try:
            payload = decrypt_credential_reference(
                settings.encryption_key, connection.credential_reference
            )
        except ValueError:
            return
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            return
        try:
            get_connection_provider(connection.provider).revoke_token(str(refresh_token))
        except Exception:
            return

    # ------------------------------------------------------------------ #
    # Credential refresh (on-demand + beat sweep)
    # ------------------------------------------------------------------ #
    def refresh_credentials(
        self,
        connection_id: UUID,
        *,
        request_context: dict[str, Any] | None = None,
    ) -> ProviderConnection:
        """Refresh the encrypted credential for a single CONNECTED connection.

        On permanent provider failure (invalid_grant → token revoked by
        Google admin/user) the connection is marked REVOKED and the
        credential reference is tombstoned. Transient failures leave the
        connection CONNECTED so the next sweep retries.
        """
        connection = self.get(connection_id)
        if connection.status not in ("CONNECTED", "ERROR"):
            raise ProviderConnectionError(
                AUTH_REQUIRED,
                "This connection is not in a refreshable state",
            )
        if not connection.credential_reference:
            raise ProviderConnectionError(
                AUTH_REQUIRED,
                "This connection has no stored credentials",
            )
        provider = get_connection_provider(connection.provider)
        try:
            result = provider.refresh_credentials(
                credential_reference=connection.credential_reference,
                credential_version=connection.credential_version,
            )
        except ProviderConnectionError as exc:
            is_permanent = exc.code == AUTH_REQUIRED
            if is_permanent:
                connection.credential_reference = invalidate_credential_reference(
                    connection.credential_version
                )
                connection.status = "REVOKED"
                connection.updated_at = datetime.now(UTC)
                self.session.commit()
            self._audit_metadata(
                connection,
                _audit_action(connection.provider, "REFRESH_FAILED"),
                connection.connected_by,
                request_context,
                reason=exc.code or "REFRESH_FAILED",
            )
            self.session.commit()
            raise
        connection.credential_reference = result.credential_reference
        connection.credential_version = result.credential_version
        connection.credential_expires_at = result.expires_at
        connection.updated_at = datetime.now(UTC)
        self._audit_metadata(
            connection,
            _audit_action(connection.provider, "REFRESHED"),
            connection.connected_by,
            request_context,
        )
        self.session.commit()
        return self.get(connection.id)

    # ------------------------------------------------------------------ #
    # Audit helpers
    # ------------------------------------------------------------------ #
    def _audit_metadata(
        self,
        connection: ProviderConnection,
        action: str | None,
        user_id: UUID | None,
        request_context: dict[str, Any] | None,
        *,
        reason: str | None = None,
        is_new: bool | None = None,
    ) -> None:
        if action is None:
            return
        metadata: dict[str, Any] = {
            "provider": connection.provider,
            "connection_id": str(connection.id),
            "workspace_domain": connection.workspace_domain,
        }
        if reason:
            metadata["reason"] = reason
        if is_new is not None:
            metadata["created"] = is_new
        if request_context:
            self._merge_request_metadata(metadata, request_context)
        self._audit(self.session, self.tenant_id, user_id, action, connection, metadata)

    def _audit_failed(
        self,
        record: OAuthStateRecord,
        reason: str,
        actor: UUID,
        request_context: dict[str, Any] | None,
        *,
        provider: str = GOOGLE_PROVIDER,
    ) -> None:
        stub = ProviderConnection(
            tenant_id=record.tenant_id,
            provider=provider,
            connection_type="OAUTH",
            status="ERROR",
            connected_by=record.user_id,
        )
        if record.connection_id is not None:
            stub.id = record.connection_id
        metadata: dict[str, Any] = {"provider": provider, "reason": reason, "connection_id": str(stub.id)}
        if request_context:
            self._merge_request_metadata(metadata, request_context)
        self._audit(
            self.session,
            record.tenant_id,
            actor,
            _audit_action(provider, "FAILED"),
            stub,
            metadata,
        )
        try:
            self.session.commit()
        except Exception:
            self.session.rollback()

    @staticmethod
    def _merge_request_metadata(metadata: dict[str, Any], request_context: dict[str, Any]) -> None:
        ip = request_context.get("ip_address")
        ua = request_context.get("user_agent")
        if ip:
            metadata["ip_address"] = str(ip)
        if ua:
            metadata["user_agent"] = str(ua)[:200]


# ------------------------------------------------------------------ #
# Tenant-agnostic sweep (called by celery beat)
# ------------------------------------------------------------------ #
def refresh_due_provider_connections(session: Session) -> dict[str, int]:
    """Scan all tenants for connections due for credential refresh.

    Returns ``{"refreshed": N, "failed": N, "revoked": N}`` for telemetry.
    Connections whose ``credential_expires_at`` is within the configured
    margin (or already past) are refreshed. Permanent failures (AUTH_REQUIRED)
    revoke the connection; transient failures are retried next cycle.
    """
    from datetime import timedelta

    margin = timedelta(minutes=settings.provider_credential_refresh_margin_minutes)
    now = datetime.now(UTC)
    due_at = now + margin

    candidates = list(
        session.scalars(
            select(ProviderConnection)
            .where(
                ProviderConnection.status.in_(["CONNECTED", "ERROR"]),
                ProviderConnection.credential_reference.isnot(None),
                ProviderConnection.credential_reference != "",
                (
                    (ProviderConnection.credential_expires_at.is_(None))
                    | (ProviderConnection.credential_expires_at <= due_at)
                ),
            )
            .order_by(ProviderConnection.credential_expires_at.asc())
            .limit(100)
        ).all()
    )

    refreshed = 0
    failed = 0
    revoked = 0
    for connection in candidates:
        reference = connection.credential_reference
        version = connection.credential_version
        if not reference or not version:
            failed += 1
            continue
        provider = get_connection_provider(connection.provider)
        try:
            result = provider.refresh_credentials(
                credential_reference=reference,
                credential_version=version,
            )
        except ProviderConnectionError as exc:
            failed += 1
            is_permanent = exc.code == AUTH_REQUIRED
            if is_permanent:
                connection.credential_reference = invalidate_credential_reference(
                    connection.credential_version
                )
                connection.status = "REVOKED"
                connection.updated_at = datetime.now(UTC)
                revoked += 1
            # Audit
            try:
                AuditService(
                    session, connection.tenant_id, connection.connected_by
                ).record(
                    _audit_action(connection.provider, "REFRESH_FAILED"),
                    EMAIL_PROVIDER_CONNECTION,
                    connection.id,
                    {"provider": connection.provider, "reason": exc.code or "REFRESH_FAILED"},
                )
            except Exception:
                pass
            session.commit()
            log_event(
                "provider_credential.refresh_failed",
                tenant_id=str(connection.tenant_id),
                connection_id=str(connection.id),
                provider=connection.provider,
                reason=exc.code or "REFRESH_FAILED",
            )
            continue

        connection.credential_reference = result.credential_reference
        connection.credential_version = result.credential_version
        connection.credential_expires_at = result.expires_at
        connection.updated_at = datetime.now(UTC)
        refreshed += 1
        try:
            AuditService(
                session, connection.tenant_id, connection.connected_by
            ).record(
                _audit_action(connection.provider, "REFRESHED"),
                EMAIL_PROVIDER_CONNECTION,
                connection.id,
                {"provider": connection.provider},
            )
        except Exception:
            pass
        session.commit()

    if candidates:
        log_event(
            "provider_credential.refresh_sweep",
            refreshed=str(refreshed),
            failed=str(failed),
            revoked=str(revoked),
            scanned=str(len(candidates)),
        )

    return {"refreshed": refreshed, "failed": failed, "revoked": revoked}


def _provider_label(provider: str) -> str:
    if provider == MICROSOFT_PROVIDER:
        return "Microsoft 365"
    return "Google Workspace"


def _parse_expiry(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None