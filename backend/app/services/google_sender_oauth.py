"""Google Workspace / Gmail sender OAuth flow (System B, Phase 10B).

Implements the consent round-trip against the Phase 10A ``SenderConnection``
model:

* ``authorization_url`` issues a cryptographically-random, single-use, expiring
  OAuth state bound to the initiating tenant + user (Redis-backed).
* ``complete`` exchanges the code with Google, binds the authenticated Google
  identity (``sub`` / verified email), stores tokens ONLY as an encrypted
  credential reference, and creates-or-reconnects a sender connection.

Security guarantees (spec item 4):

* State is consumed atomically — replay is rejected.
* The tenant/user never come from the callback URL; they come from the state.
* Granted scopes are checked against the requested set (base send-only scopes,
  or the opt-in extended set when the connection requested ``inbox_access``);
  escalation is rejected.
* Tokens never appear in responses, logs, audit records or redirects.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

import jwt as _jwt
from google_auth_oauthlib.flow import Flow
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.email_providers.credentials import (
    encrypt_credential_reference,
    rotate_credential_reference,
)
from app.email_providers.google.provider import (
    GMAIL_INBOX_OAUTH_SCOPES,
    GMAIL_OAUTH_SCOPES,
)
from app.models import SenderConnection
from app.services.audit import AuditService
from app.services.oauth_state_store import (
    OAuthStateError,
    OAuthStateRecord,
    OAuthStateStore,
    build_state_store,
)

__all__ = ["GoogleOAuthError", "GoogleSenderOAuthService"]

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"


class GoogleOAuthError(ValueError):
    pass


class GoogleSenderOAuthService:
    """Production consent flow targeting ``SenderConnection`` rows."""

    def __init__(self, session: Session, state_store: OAuthStateStore | None = None) -> None:
        self.session = session
        self._state_store_override = state_store

    # ------------------------------------------------------------------ #
    # State + client configuration
    # ------------------------------------------------------------------ #
    def _state_store(self) -> OAuthStateStore:
        return self._state_store_override or build_state_store()

    def _client_config(self) -> dict[str, Any]:
        if not settings.google_client_id or not settings.google_client_secret:
            raise GoogleOAuthError("Google OAuth is not configured")
        return {
            "web": {
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "auth_uri": GOOGLE_AUTHORIZE_URL,
                "token_uri": GOOGLE_TOKEN_URL,
                "redirect_uris": [settings.google_sender_redirect_uri],
            }
        }

    # ------------------------------------------------------------------ #
    # Step 1 — consent URL
    # ------------------------------------------------------------------ #
    def authorization_url(self, connection: SenderConnection, user_id: UUID) -> str:
        if connection.provider.upper() != "GOOGLE":
            raise GoogleOAuthError("This connection is not a Google connection")
        state = self._state_store().create(
            tenant_id=connection.tenant_id,
            user_id=user_id,
            connection_id=connection.id,
        )
        audit = AuditService(self.session, connection.tenant_id, user_id)
        audit.record(
            "GOOGLE_CONNECTION_STARTED",
            "sender_connection",
            connection.id,
            {"provider": "GOOGLE"},
        )
        flow = Flow.from_client_config(self._client_config(), scopes=list(self._scopes_for(connection)), state=state)
        flow.redirect_uri = settings.google_sender_redirect_uri
        url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="false",
        )
        return cast(str, url)

    @staticmethod
    def _scopes_for(connection: SenderConnection) -> tuple[str, ...]:
        """Opt-in scope selection: base send-only scopes by default, extended
        with ``gmail.readonly`` only when the connection requested inbox access."""
        metadata = connection.connection_metadata or {}
        if bool(metadata.get("inbox_access")):
            return GMAIL_INBOX_OAUTH_SCOPES
        return GMAIL_OAUTH_SCOPES

    # ------------------------------------------------------------------ #
    # Step 2 — callback: code exchange + identity binding
    # ------------------------------------------------------------------ #
    def complete(self, code: str, state: str) -> SenderConnection:
        try:
            record = self._state_store().consume(state)
        except OAuthStateError as exc:
            raise GoogleOAuthError(str(exc)) from exc
        expected = self._expected_scopes(record)
        flow = Flow.from_client_config(self._client_config(), scopes=list(expected), state=state)
        flow.redirect_uri = settings.google_sender_redirect_uri
        try:
            flow.fetch_token(code=code)
        except Exception as exc:
            self._audit_failed(record, "GOOGLE_CONNECTION_FAILED", "token_exchange_failed")
            raise GoogleOAuthError("Google authorization could not be completed") from exc

        credentials = flow.credentials
        _reject_scope_escalation(credentials, expected)
        sub, email, verified = self._identity(credentials, record)
        if not sub or not email:
            self._audit_failed(record, "GOOGLE_CONNECTION_FAILED", "identity_unavailable")
            raise GoogleOAuthError("Google did not return a usable account identity")

        connection, is_new = self._resolve_connection(record, sub, email)
        payload = self._token_payload(credentials)
        self._persist_tokens(connection, payload, actor_id=record.user_id)
        self._apply_connection_state(connection, sub, email, verified, credentials, actor_id=record.user_id, is_new=is_new)
        self.session.add(connection)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise GoogleOAuthError(
                "A Google connection for this account already exists; reconnect to refresh it"
            ) from exc
        return connection

    # ------------------------------------------------------------------ #
    # Identity + token payload
    # ------------------------------------------------------------------ #
    def _expected_scopes(self, record: OAuthStateRecord) -> tuple[str, ...]:
        """The scope set the callback must match, re-derived from the state's
        connection (nevers from the query string)."""
        if record.connection_id is None:
            return GMAIL_OAUTH_SCOPES
        connection = self.session.scalar(
            select(SenderConnection).where(
                SenderConnection.id == record.connection_id,
                SenderConnection.tenant_id == record.tenant_id,
            )
        )
        if connection is None:
            raise GoogleOAuthError("The connection no longer exists; please connect fresh")
        return self._scopes_for(connection)

    @staticmethod
    def _token_payload(credentials: Any) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "access_token": str(credentials.token or ""),
            "refresh_token": str(credentials.refresh_token or ""),
            "client_id": str(credentials.client_id or ""),
            "token_uri": str(credentials.token_uri or GOOGLE_TOKEN_URL),
            "scopes": sorted(str(scope) for scope in (credentials.scopes or GMAIL_OAUTH_SCOPES)),
        }
        if credentials.expiry:
            payload["expires_at"] = credentials.expiry.isoformat()
        if not payload["refresh_token"]:
            # Offline consent must yield a refresh token; a missing one means
            # Google will not be able to renew access.
            raise GoogleOAuthError("Google did not return a refresh token; please reconnect")
        return payload

    def _identity(self, credentials: Any, record: OAuthStateRecord) -> tuple[str, str, bool]:
        """Resolve ``(sub, email, email_verified)`` from the OpenID id_token."""
        id_token = getattr(credentials, "id_token", None)
        if id_token:
            try:
                claims = _jwt.decode(id_token, options={"verify_signature": False})
            except _jwt.PyJWTError:
                claims = None
            if claims and claims.get("sub"):
                email = str(claims.get("email") or "").strip().lower()
                verified = bool(claims.get("email_verified"))
                return str(claims["sub"]), email, verified
        # Fallback (should not occur with the openid scope): read the primary
        # mailbox directly from the Gmail API.
        from googleapiclient.discovery import build

        try:
            service = build("gmail", "v1", credentials=credentials, cache_discovery=False)
            profile = cast(dict[str, Any], service.users().getProfile(userId="me").execute())
        except Exception as exc:
            self._audit_failed(record, "GOOGLE_CONNECTION_FAILED", "identity_unavailable")
            raise GoogleOAuthError("Google account identity could not be resolved") from exc
        email = str(profile.get("emailAddress") or "").strip().lower()
        return email, email, False

    # ------------------------------------------------------------------ #
    # Connection resolution (duplicate protection / reconnect)
    # ------------------------------------------------------------------ #
    def _resolve_connection(self, record: OAuthStateRecord, sub: str, email: str) -> tuple[SenderConnection, bool]:
        """Return the connection to update plus whether it is brand-new."""
        if record.connection_id is not None:
            connection = self.session.scalar(
                select(SenderConnection).where(
                    SenderConnection.id == record.connection_id,
                    SenderConnection.tenant_id == record.tenant_id,
                )
            )
            if connection is None:
                raise GoogleOAuthError("The connection no longer exists; please connect fresh")
            if connection.external_account_id and connection.external_account_id not in (sub, email):
                raise GoogleOAuthError("The reconnected Google account does not match this connection")
            return connection, False
        existing = self.session.scalar(
            select(SenderConnection).where(
                SenderConnection.tenant_id == record.tenant_id,
                SenderConnection.provider == "GOOGLE",
                SenderConnection.external_account_id.in_([sub, email]),
            )
        )
        if existing is not None:
            return existing, False
        return (
            SenderConnection(
                tenant_id=record.tenant_id,
                provider="GOOGLE",
                connection_type="OAUTH",
                status="CONNECTING",
                created_by=record.user_id,
            ),
            True,
        )

    def _persist_tokens(self, connection: SenderConnection, payload: dict[str, Any], actor_id: UUID) -> None:
        try:
            if connection.credential_reference is not None:
                connection.credential_reference, connection.credential_version = rotate_credential_reference(
                    settings.encryption_key,
                    str(connection.credential_reference),
                    new_payload=payload,
                    previous_version=connection.credential_version,
                )
            else:
                connection.credential_reference = encrypt_credential_reference(
                    settings.encryption_key, payload, version="v1"
                )
                connection.credential_version = "v1"
        except ValueError as exc:
            raise GoogleOAuthError("Stored credentials could not be updated") from exc
        raw_expiry = payload.get("expires_at")
        connection.credential_expires_at = _parse_expiry(raw_expiry)

    def _apply_connection_state(
        self,
        connection: SenderConnection,
        sub: str,
        email: str,
        verified: bool,
        credentials: Any,
        actor_id: UUID,
        *,
        is_new: bool,
    ) -> None:
        now = datetime.now(UTC)
        connection.status = "CONNECTED"
        connection.external_account_id = sub
        connection.email = email
        connection.created_by = connection.created_by or actor_id
        connection.last_connected_at = now
        metadata = dict(connection.connection_metadata or {})
        metadata["google_sub"] = sub
        metadata["email_verified"] = bool(verified)
        metadata["scopes"] = sorted(str(scope) for scope in (credentials.scopes or GMAIL_OAUTH_SCOPES))
        connection.connection_metadata = metadata
        audit = AuditService(self.session, connection.tenant_id, actor_id)
        if is_new:
            audit.record(
                "GOOGLE_CONNECTION_CREATED",
                "sender_connection",
                connection.id,
                {"provider": "GOOGLE", "account": email},
            )
        else:
            audit.record(
                "GOOGLE_CONNECTION_VALIDATED",
                "sender_connection",
                connection.id,
                {"provider": "GOOGLE", "reconnected": True, "account": email},
            )

    # ------------------------------------------------------------------ #
    # Failure auditing (never any secrets)
    # ------------------------------------------------------------------ #
    def _audit_failed(self, record: OAuthStateRecord, action: str, reason: str) -> None:
        AuditService(self.session, record.tenant_id, record.user_id).record(
            action,
            "sender_connection",
            record.connection_id,
            {"provider": "GOOGLE", "reason": reason},
        )
        self.session.commit()


def _reject_scope_escalation(credentials: Any, requested: tuple[str, ...]) -> None:
    requested_scopes = {str(scope) for scope in requested}
    granted = {
        str(scope).strip()
        for scope in (credentials.scopes or [])
        if str(scope).strip()
    }
    if not granted <= requested_scopes:
        raise GoogleOAuthError("Google granted scopes outside the requested set")


def _parse_expiry(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None
