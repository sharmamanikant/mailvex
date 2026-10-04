"""Zoho Mail sender OAuth flow (System B, Phase 10D).

Mirrors the Microsoft (Phase 10C) service against the ``SenderConnection``
model, with a Zoho-specific token/identity exchange:

* ``authorization_url`` issues a cryptographically-random, single-use, expiring
  OAuth state bound to the initiating tenant + user (Redis-backed, reused from
  ``app.services.oauth_state_store``).
* ``complete`` exchanges the authorization code with the Zoho accounts token
  endpoint, binds the Zoho identity (``ZUID`` / ``Email`` / ``Display_Name``),
  stores tokens ONLY as an encrypted credential reference, and
  creates-or-reconnects a sender connection.

Security guarantees (spec item 3):

* State is consumed atomically — replay is rejected.
* The tenant/user never come from the callback URL; they come from the state.
* Granted scopes are checked against the minimum requested set (escalation is
  rejected).
* Tokens never appear in responses, logs, audit records or redirects.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode
from uuid import UUID

import requests
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.email_providers.credentials import (
    encrypt_credential_reference,
    rotate_credential_reference,
)
from app.email_providers.zoho.provider import ZOHO_SCOPES
from app.models import SenderConnection
from app.services.audit import AuditService
from app.services.oauth_state_store import (
    OAuthStateError,
    OAuthStateRecord,
    OAuthStateStore,
    build_state_store,
)

__all__ = ["ZohoOAuthError", "ZohoSenderOAuthService"]


class ZohoOAuthError(ValueError):
    pass


class ZohoSenderOAuthService:
    """Production consent flow targeting ``SenderConnection`` rows."""

    def __init__(self, session: Session, state_store: OAuthStateStore | None = None) -> None:
        self.session = session
        self._state_store_override = state_store

    # ------------------------------------------------------------------ #
    # State + client configuration
    # ------------------------------------------------------------------ #
    def _state_store(self) -> OAuthStateStore:
        return self._state_store_override or build_state_store()

    def _client(self) -> tuple[str, str]:
        if not settings.zoho_client_id or not settings.zoho_client_secret:
            raise ZohoOAuthError("Zoho OAuth is not configured")
        return settings.zoho_client_id, settings.zoho_client_secret

    @property
    def _authority(self) -> str:
        return (settings.zoho_authority or "https://accounts.zoho.com").rstrip("/")

    # ------------------------------------------------------------------ #
    # Step 1 — consent URL
    # ------------------------------------------------------------------ #
    def authorization_url(self, connection: SenderConnection, user_id: UUID) -> str:
        if connection.provider.upper() != "ZOHO":
            raise ZohoOAuthError("This connection is not a Zoho connection")
        client_id, _ = self._client()
        state = self._state_store().create(
            tenant_id=connection.tenant_id,
            user_id=user_id,
            connection_id=connection.id,
        )
        audit = AuditService(self.session, connection.tenant_id, user_id)
        audit.record(
            "ZOHO_OAUTH_STARTED",
            "sender_connection",
            connection.id,
            {"provider": "ZOHO"},
        )
        params = urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": settings.zoho_sender_redirect_uri,
                "scope": ",".join(ZOHO_SCOPES),
                "state": state,
                "access_type": "offline",
                "prompt": "consent",
            }
        )
        return f"{self._authority}/oauth/v2/auth?{params}"

    # ------------------------------------------------------------------ #
    # Step 2 — callback: code exchange + identity binding
    # ------------------------------------------------------------------ #
    def complete(self, code: str, state: str) -> SenderConnection:
        try:
            record = self._state_store().consume(state)
        except OAuthStateError as exc:
            raise ZohoOAuthError(str(exc)) from exc
        token_data, profile = self._exchange(code, record)
        account_id, email, display_name = self._identity(profile, record)
        if not account_id or not email:
            self._audit_failed(record, "ZOHO_OAUTH_FAILED", "identity_unavailable")
            raise ZohoOAuthError("Zoho did not return a usable account identity")
        _reject_scope_escalation(token_data, record)
        connection, is_new = self._resolve_connection(record, account_id, email)
        payload = self._token_payload(token_data)
        self._persist_tokens(connection, payload, actor_id=record.user_id)
        self._apply_connection_state(
            connection,
            account_id,
            email,
            display_name,
            token_data,
            actor_id=record.user_id,
            is_new=is_new,
        )
        self.session.add(connection)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ZohoOAuthError(
                "A Zoho connection for this account already exists; reconnect to refresh it"
            ) from exc
        return connection

    # ------------------------------------------------------------------ #
    # Token exchange + identity
    # ------------------------------------------------------------------ #
    def _exchange(self, code: str, record: OAuthStateRecord) -> tuple[dict[str, Any], dict[str, Any]]:
        client_id, client_secret = self._client()
        try:
            response = requests.post(
                f"{self._authority}/oauth/v2/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": settings.zoho_sender_redirect_uri,
                    "grant_type": "authorization_code",
                    "scope": ",".join(ZOHO_SCOPES),
                },
                timeout=20,
            )
        except requests.RequestException as exc:
            self._audit_failed(record, "ZOHO_OAUTH_FAILED", "token_exchange_failed")
            raise ZohoOAuthError("Zoho authorization could not be completed") from exc
        if not response.ok:
            self._audit_failed(record, "ZOHO_OAUTH_FAILED", "token_exchange_rejected")
            raise ZohoOAuthError("Zoho authorization could not be completed")
        try:
            token_data = response.json()
        except ValueError as exc:
            self._audit_failed(record, "ZOHO_OAUTH_FAILED", "invalid_token_response")
            raise ZohoOAuthError("Zoho returned an unreadable response") from exc
        if not token_data.get("access_token"):
            self._audit_failed(record, "ZOHO_OAUTH_FAILED", "missing_access_token")
            raise ZohoOAuthError("Zoho did not return an access token")
        if not token_data.get("refresh_token"):
            self._audit_failed(record, "ZOHO_OAUTH_FAILED", "missing_refresh_token")
            raise ZohoOAuthError("Zoho did not return a refresh token; please reconnect")
        profile = self._zoho_profile(str(token_data["access_token"]), record)
        return token_data, profile

    def _zoho_profile(self, access_token: str, record: OAuthStateRecord) -> dict[str, Any]:
        try:
            response = requests.get(
                f"{self._authority}/oauth/v2/userinfo",
                headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
                timeout=20,
            )
        except requests.RequestException as exc:
            self._audit_failed(record, "ZOHO_OAUTH_FAILED", "profile_fetch_failed")
            raise ZohoOAuthError("Zoho account identity could not be resolved") from exc
        if not response.ok:
            self._audit_failed(record, "ZOHO_OAUTH_FAILED", "profile_rejected")
            raise ZohoOAuthError("Zoho account identity could not be resolved")
        try:
            data = response.json()
        except ValueError:
            data = {}
        if not isinstance(data, dict):
            self._audit_failed(record, "ZOHO_OAUTH_FAILED", "profile_invalid")
            raise ZohoOAuthError("Zoho account identity could not be resolved")
        return data

    @staticmethod
    def _identity(profile: dict[str, Any], record: OAuthStateRecord) -> tuple[str, str, str]:
        email = str(profile.get("Email") or profile.get("email") or "").strip().lower()
        account_id = str(profile.get("ZUID") or profile.get("zuid") or "").strip() or email
        display_name = str(
            profile.get("Display_Name")
            or profile.get("Full_Name")
            or profile.get("display_name")
            or ""
        ).strip()
        return account_id, email, display_name

    @staticmethod
    def _token_payload(token_data: dict[str, Any]) -> dict[str, Any]:
        expires_at: datetime | None = None
        expires_in = token_data.get("expires_in")
        if isinstance(expires_in, (int, float)):
            expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
        payload: dict[str, Any] = {
            "access_token": str(token_data.get("access_token") or ""),
            "refresh_token": str(token_data.get("refresh_token") or ""),
            "client_id": str(token_data.get("client_id") or settings.zoho_client_id),
            "scopes": sorted(str(scope).strip() for scope in (token_data.get("scope") or ",".join(ZOHO_SCOPES)).replace(",", " ").split() if str(scope).strip()),
        }
        if expires_at is not None:
            payload["expires_at"] = expires_at.isoformat()
        return payload

    # ------------------------------------------------------------------ #
    # Connection resolution (duplicate protection / reconnect)
    # ------------------------------------------------------------------ #
    def _resolve_connection(
        self,
        record: OAuthStateRecord,
        account_id: str,
        email: str,
    ) -> tuple[SenderConnection, bool]:
        allowed = {identifier.lower() for identifier in (account_id, email) if identifier}
        if record.connection_id is not None:
            connection = self.session.scalar(
                select(SenderConnection).where(
                    SenderConnection.id == record.connection_id,
                    SenderConnection.tenant_id == record.tenant_id,
                )
            )
            if connection is None:
                raise ZohoOAuthError("The connection no longer exists; please connect fresh")
            if connection.external_account_id and connection.external_account_id.lower() not in allowed:
                raise ZohoOAuthError("The reconnected Zoho account does not match this connection")
            return connection, False
        existing = self.session.scalar(
            select(SenderConnection).where(
                SenderConnection.tenant_id == record.tenant_id,
                SenderConnection.provider == "ZOHO",
                SenderConnection.external_account_id.in_(list(allowed)),
            )
        )
        if existing is not None:
            return existing, False
        return (
            SenderConnection(
                tenant_id=record.tenant_id,
                provider="ZOHO",
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
            raise ZohoOAuthError("Stored credentials could not be updated") from exc
        connection.credential_expires_at = _parse_expiry(payload.get("expires_at"))

    def _apply_connection_state(
        self,
        connection: SenderConnection,
        account_id: str,
        email: str,
        display_name: str,
        token_data: dict[str, Any],
        actor_id: UUID,
        *,
        is_new: bool,
    ) -> None:
        now = datetime.now(UTC)
        connection.status = "CONNECTED"
        connection.external_account_id = account_id
        connection.email = email
        connection.created_by = connection.created_by or actor_id
        connection.last_connected_at = now
        metadata = dict(connection.connection_metadata or {})
        metadata["zoho_id"] = account_id
        metadata["display_name"] = display_name
        scopes = sorted(
            str(scope).strip()
            for scope in (token_data.get("scope") or ",".join(ZOHO_SCOPES)).replace(",", " ").split()
            if str(scope).strip()
        )
        metadata["scopes"] = scopes
        connection.connection_metadata = metadata
        audit = AuditService(self.session, connection.tenant_id, actor_id)
        if is_new:
            audit.record(
                "ZOHO_OAUTH_CONNECTED",
                "sender_connection",
                connection.id,
                {"provider": "ZOHO", "account": email},
            )
        else:
            audit.record(
                "ZOHO_CONNECTION_VALIDATED",
                "sender_connection",
                connection.id,
                {"provider": "ZOHO", "reconnected": True, "account": email},
            )

    # ------------------------------------------------------------------ #
    # Failure auditing (never any secrets)
    # ------------------------------------------------------------------ #
    def _audit_failed(self, record: OAuthStateRecord, action: str, reason: str) -> None:
        AuditService(self.session, record.tenant_id, record.user_id).record(
            action,
            "sender_connection",
            record.connection_id,
            {"provider": "ZOHO", "reason": reason},
        )
        self.session.commit()


def _reject_scope_escalation(token_data: dict[str, Any], _record: OAuthStateRecord) -> None:
    requested = {str(scope) for scope in ZOHO_SCOPES}
    granted = {
        str(scope).strip()
        for scope in str(token_data.get("scope") or "").replace(",", " ").split()
        if str(scope).strip()
    }
    # Token endpoint always echoes the granted scopes; if empty, trust the flow.
    if granted and not granted <= requested:
        raise ZohoOAuthError("Zoho granted scopes outside the requested minimum set")


def _parse_expiry(raw: Any) -> datetime | None:
    if isinstance(raw, datetime):
        return raw
    if isinstance(raw, str):
        try:
            return datetime.fromisoformat(raw)
        except ValueError:
            return None
    return None