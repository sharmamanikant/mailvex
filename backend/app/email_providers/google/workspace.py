"""Google Workspace provider-connection adapter (Phase 1).

Implements the organization-level connection flow against ``ProviderConnection``
rows. The individual-sender flow (``GoogleSenderOAuthService`` -> Gmail API)
remains separate: this adapter authorizes a *Workspace admin* with read-only
Directory access so Phase 2 can discover Workspace mailboxes without a fresh
consent round-trip.

Security guarantees:

* The OAuth state comes from the service layer's single-use state store; the
  adapter never invents state or trusts state bound to the query string.
* Granted scopes must be a subset of the requested set - escalation is
  rejected with a user-safe error.
* Identity is resolved from the OpenID id_token (``sub``, ``email``,
  ``email_verified``, ``hd``); the hosted-domain claim is preferred for
  ``workspace_domain`` and is validated in Phase 2.
* Raw tokens are returned to the service boundary ONLY inside
  :class:`OAuthCallbackResult` and are encrypted immediately; they never reach
  API responses, audit records or logs.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime
from typing import Any, cast

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow

from app.core.config import settings
from app.email_providers.base import CredentialRotationResult
from app.email_providers.connection_base import (
    AUTH_REQUIRED,
    IDENTITY_FAILED,
    REFRESH_FAILED,
    EmailProviderConnection,
    OAuthCallbackResult,
    ProviderConnectionError,
    ProviderIdentity,
    _register_connection_provider,
)
from app.email_providers.credentials import (
    decrypt_credential_reference,
    encrypt_credential_reference,
    future_version,
)

__all__ = [
    "GOOGLE_WORKSPACE_OAUTH_SCOPES",
    "GOOGLE_WORKSPACE_PROVIDER_NAME",
    "GOOGLE_WORKSPACE_SCOPES_REQUIRED",
    "GoogleWorkspaceProviderConnection",
]

GOOGLE_WORKSPACE_PROVIDER_NAME = "GOOGLE"

GOOGLE_AUTHORIZE_URL = "https://accounts.google.com/o/oauth2/auth"
GOOGLE_TOKEN_URL = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE_URL = "https://oauth2.googleapis.com/revoke"

# Read-only, minimum-scope consent set for a Workspace connection:
#  * identity scopes to verify the acting account/domain on the callback.
#  * ``admin.directory.user.readonly`` to enumerate Workspace mailboxes in
#    Phase 2. Sending still happens per-mailbox (Phase 3+) and is never
#    authorized at the workspace level.
GOOGLE_WORKSPACE_OAUTH_SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/admin.directory.user.readonly",
)

# Scopes a Workspace connection must always possess to be considered CONNECTED.
GOOGLE_WORKSPACE_SCOPES_REQUIRED: frozenset[str] = frozenset(GOOGLE_WORKSPACE_OAUTH_SCOPES)


class GoogleWorkspaceProviderConnection(EmailProviderConnection):
    provider_name = GOOGLE_WORKSPACE_PROVIDER_NAME

    # ------------------------------------------------------------------ #
    # Client / flow plumbing
    # ------------------------------------------------------------------ #
    def _client_config(self) -> dict[str, Any]:
        if not settings.google_client_id or not settings.google_client_secret:
            raise ProviderConnectionError("NOT_CONFIGURED", "Google OAuth is not configured")
        return {
            "web": {
                "client_id": settings.google_client_id,
                "client_secret": settings.google_client_secret,
                "auth_uri": GOOGLE_AUTHORIZE_URL,
                "token_uri": GOOGLE_TOKEN_URL,
                "redirect_uris": [settings.google_workspace_redirect_uri],
            }
        }

    def _flow(self, *, state: str, scopes: tuple[str, ...]) -> Flow:
        flow = Flow.from_client_config(self._client_config(), scopes=list(scopes), state=state)
        flow.redirect_uri = settings.google_workspace_redirect_uri
        return flow

    # ------------------------------------------------------------------ #
    # Interface
    # ------------------------------------------------------------------ #
    def get_authorization_url(self, *, state: str) -> str:
        flow = self._flow(state=state, scopes=GOOGLE_WORKSPACE_OAUTH_SCOPES)
        url, _ = flow.authorization_url(
            access_type="offline",
            prompt="consent",
            include_granted_scopes="false",
        )
        if not url:
            raise ProviderConnectionError(
                "NOT_CONFIGURED",
                "Google could not build an authorization request",
            )
        return cast(str, url)

    def handle_callback(
        self,
        *,
        code: str,
        state: str,
        expected_scopes: frozenset[str],
    ) -> OAuthCallbackResult:
        if not code or not state:
            raise ProviderConnectionError(
                "STATE_INVALID",
                "Google authorization was cancelled.",
            )
        flow = self._flow(state=state, scopes=GOOGLE_WORKSPACE_OAUTH_SCOPES)
        try:
            flow.fetch_token(code=code)
        except Exception as exc:  # e.g. invalid_grant / network failure
            raise ProviderConnectionError(
                "OAUTH_EXCHANGE_FAILED",
                "Google authorization failed.",
            ) from exc

        credentials = flow.credentials
        _reject_scope_escalation(credentials, expected_scopes)
        _reject_missing_required_scopes(credentials, expected_scopes)
        identity = _identity_from(credentials)
        payload = _token_payload(credentials)
        if identity is None or not payload.get("refresh_token"):
            raise ProviderConnectionError(
                IDENTITY_FAILED,
                "The Google connection could not be verified.",
            )
        return OAuthCallbackResult(identity=identity, credential_payload=payload)

    def refresh_credentials(
        self,
        *,
        credential_reference: str,
        credential_version: str,
    ) -> CredentialRotationResult:
        payload = dict(
            decrypt_credential_reference(settings.encryption_key, credential_reference)
        )
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            raise ProviderConnectionError(
                AUTH_REQUIRED,
                "This connection has no refresh token; reconnect required",
            )
        raw_scopes = payload.get("scopes")
        credentials = Credentials(
            token=str(payload.get("access_token") or ""),
            refresh_token=str(refresh_token),
            token_uri=str(payload.get("token_uri") or GOOGLE_TOKEN_URL),
            client_id=str(payload.get("client_id") or settings.google_client_id),
            client_secret=str(settings.google_client_secret),
            scopes=(
                GOOGLE_WORKSPACE_OAUTH_SCOPES
                if not isinstance(raw_scopes, (list, tuple))
                else [str(scope) for scope in raw_scopes]
            ),
        )  # type: ignore[no-untyped-call]
        try:
            credentials.refresh(GoogleRequest())  # type: ignore[no-untyped-call]
        except Exception as exc:
            # Distinguish permanent (invalid_grant — token revoked by user/admin)
            # from transient (network, Google 5xx) failures.
            msg = str(exc).lower()
            if "invalid_grant" in msg or "token has been expired or revoked" in msg:
                raise ProviderConnectionError(
                    AUTH_REQUIRED,
                    "Google authorization expired; reconnect required",
                ) from exc
            raise ProviderConnectionError(
                REFRESH_FAILED,
                "Temporary failure refreshing Google credentials",
            ) from exc
        if not credentials.token:
            raise ProviderConnectionError(
                REFRESH_FAILED,
                "Google did not return an access token",
            )
        new_payload: dict[str, Any] = {
            "access_token": credentials.token,
            "refresh_token": credentials.refresh_token or refresh_token,
            "client_id": credentials.client_id or settings.google_client_id,
            "token_uri": credentials.token_uri or GOOGLE_TOKEN_URL,
            "scopes": sorted(
                str(scope) for scope in (credentials.scopes or GOOGLE_WORKSPACE_OAUTH_SCOPES)
            ),
        }
        if credentials.expiry:
            new_payload["expires_at"] = credentials.expiry.isoformat()
        try:
            new_version = future_version(credential_version)
        except ValueError:
            new_version = "v1"
        reference = encrypt_credential_reference(settings.encryption_key, new_payload, version=new_version)
        return CredentialRotationResult(
            credential_reference=reference,
            credential_version=new_version,
            expires_at=credentials.expiry,
        )

    def disconnect(self) -> None:
        # Workspace-level disconnect is a local operation: the service layer
        # revokes the refresh token best-effort and tombstones the reference.
        return None

    # ------------------------------------------------------------------ #
    # Best-effort server-side token revocation (never exposed to the client)
    # ------------------------------------------------------------------ #
    def revoke_token(self, refresh_token: str) -> None:
        """Ask Google to revoke a refresh token. Never raises, never logs."""
        import requests

        try:
            response = requests.post(GOOGLE_REVOKE_URL, data={"token": refresh_token}, timeout=10)
        except Exception:
            return None
        # 200 = revoked, 400 = already revoked/invalid; both satisfy the goal.
        if response.status_code not in (200, 400, 410):
            return None
        return None


_register_connection_provider(GoogleWorkspaceProviderConnection)


def _reject_scope_escalation(credentials: Any, requested: frozenset[str]) -> None:
    granted = {
        str(scope).strip()
        for scope in (credentials.scopes or [])
        if str(scope).strip()
    }
    if not granted <= requested:
        raise ProviderConnectionError(
            "INSUFFICIENT_SCOPE",
            "Required permissions were not granted.",
        )


def _reject_missing_required_scopes(credentials: Any, required: frozenset[str]) -> None:
    granted = {
        str(scope).strip()
        for scope in (credentials.scopes or [])
        if str(scope).strip()
    }
    if not required <= granted:
        raise ProviderConnectionError(
            "INSUFFICIENT_SCOPE",
            "Required permissions were not granted.",
        )


def _identity_from(credentials: Any) -> ProviderIdentity | None:
    """Resolve the verified Workspace identity from the OpenID id_token."""
    id_token = getattr(credentials, "id_token", None)
    claims = _decode_id_token(id_token) if id_token else None
    sub = str((claims or {}).get("sub") or "")
    email = str((claims or {}).get("email") or "").strip().lower()
    if not sub or not email:
        return None
    hd = str((claims or {}).get("hd") or "").strip().lower() or None
    workspace_domain = hd or _domain_of(email)
    display_name = (
        f"Google Workspace ({workspace_domain})" if workspace_domain else email.split("@")[0]
    )
    scopes = {
        str(scope).strip()
        for scope in (credentials.scopes or [])
        if str(scope).strip()
    }
    return ProviderIdentity(
        provider=GOOGLE_WORKSPACE_PROVIDER_NAME,
        provider_account_id=sub,
        email=email,
        workspace_domain=workspace_domain,
        display_name=display_name,
        scopes=tuple(sorted(scopes)),
    )


def _domain_of(email: str) -> str | None:
    return email.partition("@")[2].strip() or None


def _decode_id_token(id_token: str) -> dict[str, Any] | None:
    """Decode id_token claims without verifying the signature.

    Signature verification is unnecessary: the token arrives over the verified
    server-side token-endpoint exchange (TLS) and the ``sub`` claim is later
    cross-checked against the state-bound connection.
    """
    try:
        segment = id_token.split(".")[1]
        padding = "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(segment + padding).decode("utf-8")
        claims = json.loads(raw)
    except (IndexError, ValueError, TypeError, UnicodeDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def _token_payload(credentials: Any) -> dict[str, Any]:
    expiry = _expiry(credentials)
    payload: dict[str, Any] = {
        "access_token": str(credentials.token or ""),
        "refresh_token": str(credentials.refresh_token or ""),
        "client_id": str(credentials.client_id or ""),
        "token_uri": str(credentials.token_uri or GOOGLE_TOKEN_URL),
        "scopes": sorted(
            str(scope) for scope in (credentials.scopes or GOOGLE_WORKSPACE_OAUTH_SCOPES)
        ),
    }
    if expiry is not None:
        payload["expires_at"] = expiry.isoformat()
    return payload


def _expiry(credentials: Any) -> datetime | None:
    expiry = getattr(credentials, "expiry", None)
    if isinstance(expiry, datetime):
        return expiry if expiry.tzinfo is not None else expiry.replace(tzinfo=UTC)
    if isinstance(expiry, str):
        try:
            parsed = datetime.fromisoformat(expiry)
        except ValueError:
            return None
        return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)
    return None