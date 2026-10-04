"""Microsoft 365 org-level provider-connection adapter (Phase 6).

Implements the organization-level consent flow against ``ProviderConnection``
rows for a Microsoft 365 / Entra ID tenant. It mirrors
``app.email_providers.google.workspace.GoogleWorkspaceProviderConnection``:

* The OAuth state comes from the service layer's single-use state store; the
  adapter never invents state or trusts state bound to the query string.
* The authority is multi-tenant (``organizations``), so any Azure AD tenant
  admin can consent; the authenticated tenant is recovered from the OpenID
  id_token ``tid`` claim — never from the query string or user input.
* ``provider_account_id`` is the **Microsoft tenant id** (``tid``) because the
  connection represents the *tenant org*, not the consenting admin. The
  existing ``(tenant_id, provider, provider_account_id)`` uniqueness index then
  prevents a second connection for the same M365 tenant.
* Granted scopes must be within the requested set (escalation is rejected)
  and must cover the required set (with ``Directory.Read.All`` +
  ``Organization.Read.All`` a tenant admin must consent — surfaced as
  ``ADMIN_CONSENT_REQUIRED`` when Azure refuses).
* Org metadata (``organizationName`` / ``defaultDomain`` /
  ``microsoftTenantId`` / consenting admin object id) is returned in
  ``OAuthCallbackResult.connection_metadata`` — non-secret data persisted
  alongside the connection.
* Raw tokens are returned to the service boundary ONLY inside
  :class:`OAuthCallbackResult.credential_payload` and are encrypted
  immediately; they never reach API responses, audit records or logs.
"""

from __future__ import annotations

import base64
import json
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import urlencode

import requests

from app.core.config import settings
from app.email_providers.base import CredentialRotationResult
from app.email_providers.connection_base import (
    AUTH_REQUIRED,
    IDENTITY_FAILED,
    INSUFFICIENT_SCOPE,
    NOT_CONFIGURED,
    OAUTH_EXCHANGE_FAILED,
    REFRESH_FAILED,
    STATE_INVALID,
    EmailProviderConnection,
    OAuthCallbackResult,
    ProviderConnectionError,
    ProviderIdentity,
)
from app.email_providers.credentials import (
    decrypt_credential_reference,
    encrypt_credential_reference,
    future_version,
)
from app.email_providers.microsoft.graph_client import MicrosoftGraphClient

__all__ = [
    "MICROSOFT_WORKSPACE_OAUTH_SCOPES",
    "MICROSOFT_WORKSPACE_PROVIDER_NAME",
    "MICROSOFT_WORKSPACE_SCOPES_REQUIRED",
    "MicrosoftWorkspaceProviderConnection",
]

MICROSOFT_WORKSPACE_PROVIDER_NAME = "MICROSOFT"

# Minimum consent set for an org-level Microsoft 365 connection:
#  * identity scopes to verify the acting account/tenant on the callback.
#  * ``User.Read`` for basic profile verification.
#  * ``Directory.Read.All`` to enumerate tenant mailboxes in mailbox sync.
#  * ``Organization.Read.All`` to resolve the org display name + default domain.
#  * ``offline_access`` to persist a refresh token for rotation.
MICROSOFT_WORKSPACE_OAUTH_SCOPES: tuple[str, ...] = (
    "openid",
    "profile",
    "email",
    "offline_access",
    "User.Read",
    "Directory.Read.All",
    "Organization.Read.All",
)

# Scopes a Microsoft 365 connection must always possess to be CONNECTED.
MICROSOFT_WORKSPACE_SCOPES_REQUIRED: frozenset[str] = frozenset(MICROSOFT_WORKSPACE_OAUTH_SCOPES)

# Microsoft has no public OAuth "revoke a refresh token" endpoint like Google's
# ``/revoke``. Consent is managed in Entra (admin can revoke user tokens /
# revoke app consent); disabling the app registration kills the tokens. So the
# best-effort remote revoke is intentionally a no-op on the Microsoft side.
MICROSOFT_REVOKE_NOOP = True


class MicrosoftWorkspaceProviderConnection(EmailProviderConnection):
    provider_name = MICROSOFT_WORKSPACE_PROVIDER_NAME

    # ------------------------------------------------------------------ #
    # Client / flow plumbing
    # ------------------------------------------------------------------ #
    @property
    def _authority(self) -> str:
        base = (settings.microsoft_authority or "https://login.microsoftonline.com").rstrip("/")
        configured = (settings.microsoft_tenant_id or "common").strip()
        if configured.lower() in ("common", "consumers", ""):
            # Organization accounts only — consumer-hosted live.com accounts
            # can never be a CR+CRM workspace mailbox source.
            tenant = "organizations"
        else:
            tenant = configured
        return f"{base}/{tenant}"

    def _client_config(self) -> tuple[str, str]:
        if not settings.microsoft_client_id or not settings.microsoft_client_secret:
            raise ProviderConnectionError(NOT_CONFIGURED, "Microsoft OAuth is not configured")
        return settings.microsoft_client_id, settings.microsoft_client_secret

    # ------------------------------------------------------------------ #
    # Interface
    # ------------------------------------------------------------------ #
    def get_authorization_url(self, *, state: str) -> str:
        client_id, _ = self._client_config()
        params = urlencode(
            {
                "client_id": client_id,
                "response_type": "code",
                "redirect_uri": settings.microsoft_workspace_redirect_uri,
                "response_mode": "query",
                "scope": " ".join(MICROSOFT_WORKSPACE_OAUTH_SCOPES),
                "state": state,
                "prompt": "login consent",
            }
        )
        return f"{self._authority}/oauth2/v2.0/authorize?{params}"

    def handle_callback(
        self,
        *,
        code: str,
        state: str,
        expected_scopes: frozenset[str],
    ) -> OAuthCallbackResult:
        if not code or not state:
            raise ProviderConnectionError(
                STATE_INVALID,
                "Microsoft authorization was cancelled.",
            )
        token_data = self._exchange_code(code)
        _validate_scopes(token_data, expected_scopes)
        identity, connection_metadata = self._identity(token_data)
        payload = _token_payload(token_data, expected_scopes)
        if identity is None or not payload.get("refresh_token"):
            raise ProviderConnectionError(
                IDENTITY_FAILED,
                "The Microsoft connection could not be verified.",
            )
        return OAuthCallbackResult(
            identity=identity,
            credential_payload=payload,
            connection_metadata=connection_metadata,
        )

    # ------------------------------------------------------------------ #
    # Token exchange / identity
    # ------------------------------------------------------------------ #
    def _exchange_code(self, code: str) -> dict[str, Any]:
        client_id, client_secret = self._client_config()
        try:
            response = requests.post(
                f"{self._authority}/oauth2/v2.0/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "code": code,
                    "redirect_uri": settings.microsoft_workspace_redirect_uri,
                    "grant_type": "authorization_code",
                    "scope": " ".join(MICROSOFT_WORKSPACE_OAUTH_SCOPES),
                },
                timeout=20,
            )
        except requests.RequestException as exc:
            raise ProviderConnectionError(
                OAUTH_EXCHANGE_FAILED,
                "Microsoft authorization failed.",
            ) from exc
        if not response.ok:
            error_code = _token_error_code(response)
            if error_code in ("access_denied", "user_cancelled"):
                raise ProviderConnectionError(
                    "SCOPE_CANCELLED",
                    "Microsoft authorization was cancelled.",
                )
            if error_code in ("consent_required", "admin_consent_required", "interaction_required"):
                raise ProviderConnectionError(
                    "ADMIN_CONSENT_REQUIRED",
                    "A Microsoft admin must approve access to your organization.",
                )
            raise ProviderConnectionError(
                OAUTH_EXCHANGE_FAILED,
                "Microsoft authorization failed.",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderConnectionError(
                OAUTH_EXCHANGE_FAILED,
                "Microsoft returned an unreadable response.",
            ) from exc
        if not isinstance(data, dict):
            raise ProviderConnectionError(
                OAUTH_EXCHANGE_FAILED,
                "Microsoft returned an unreadable response.",
            )
        if not data.get("access_token"):
            raise ProviderConnectionError(
                OAUTH_EXCHANGE_FAILED,
                "Microsoft did not return an access token.",
            )
        if not data.get("refresh_token"):
            raise ProviderConnectionError(
                IDENTITY_FAILED,
                "Microsoft did not return a refresh token; please reconnect.",
            )
        return data

    def _identity(self, token_data: dict[str, Any]) -> tuple[ProviderIdentity | None, dict[str, Any]]:
        """Resolve identity from the id_token ``tid``/``oid``/+ org metadata."""
        claims = _decode_id_token(token_data.get("id_token"))
        if claims is None:
            return None, {}
        tid = str(claims.get("tid") or "").strip()
        oid = str(claims.get("oid") or "").strip()
        email = str(
            claims.get("email") or claims.get("preferred_username") or ""
        ).strip().lower()
        name = str(claims.get("name") or "").strip()
        if not tid or not email:
            return None, {}

        org = self._organization(token_data.get("access_token"))
        organization_name = str(org.get("displayName") or "").strip() or "Microsoft 365"
        default_domain = str(org.get("defaultDomain") or "").strip().lower() or None
        workspace_domain = default_domain or _domain_of(email)

        granted = {
            str(scope).strip()
            for scope in str(token_data.get("scope") or "").split()
            if str(scope).strip()
        }
        identity = ProviderIdentity(
            provider=MICROSOFT_WORKSPACE_PROVIDER_NAME,
            provider_account_id=tid,
            email=email,
            workspace_domain=workspace_domain,
            display_name=f"Microsoft 365 ({organization_name})" if organization_name else "Microsoft 365",
            scopes=tuple(sorted(granted)),
        )
        connection_metadata: dict[str, Any] = {
            "microsoftTenantId": tid,
            "organizationName": organization_name,
            "defaultDomain": default_domain,
            "consent_admin_object_id": oid,
            "consent_admin_email": email,
            "consent_admin_display_name": name,
        }
        return identity, connection_metadata

    def _organization(self, access_token: Any) -> dict[str, Any]:
        """Resolve org display name + default verified domain via Graph."""
        if not isinstance(access_token, str) or not access_token:
            return {}
        try:
            client = MicrosoftGraphClient(access_token)
            data = client.get("/organization?$select=id,displayName,verifiedDomains")
        except Exception:
            return {}
        values = data.get("value")
        if not isinstance(values, list) or not values:
            return {}
        org = values[0]
        if not isinstance(org, dict):
            return {}
        display_name = str(org.get("displayName") or "")
        default_domain: str | None = None
        for domain in org.get("verifiedDomains") or []:
            if isinstance(domain, dict) and bool(domain.get("isDefault")):
                candidate = str(domain.get("name") or "").strip().lower()
                if candidate:
                    default_domain = candidate
                    break
        return {"displayName": display_name, "defaultDomain": default_domain}

    # ------------------------------------------------------------------ #
    # Credential rotation
    # ------------------------------------------------------------------ #
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
        client_id, client_secret = self._client_config()
        try:
            response = requests.post(
                f"{self._authority}/oauth2/v2.0/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": str(refresh_token),
                    "grant_type": "refresh_token",
                    "scope": " ".join(MICROSOFT_WORKSPACE_OAUTH_SCOPES),
                },
                timeout=20,
            )
        except requests.RequestException as exc:
            raise ProviderConnectionError(
                REFRESH_FAILED,
                "Temporary failure refreshing Microsoft credentials",
            ) from exc
        if response.status_code in (400, 401):
            error_code = _token_error_code(response)
            if error_code in ("invalid_grant", "unauthorized_client", "interaction_required"):
                raise ProviderConnectionError(
                    AUTH_REQUIRED,
                    "Microsoft authorization expired; reconnect required",
                ) from None
            raise ProviderConnectionError(
                REFRESH_FAILED,
                "Temporary failure refreshing Microsoft credentials",
            ) from None
        if not response.ok:
            raise ProviderConnectionError(
                REFRESH_FAILED,
                "Temporary failure refreshing Microsoft credentials",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise ProviderConnectionError(
                REFRESH_FAILED,
                "Microsoft returned an unreadable token response.",
            ) from exc
        access_token = str(data.get("access_token") or "")
        if not access_token:
            raise ProviderConnectionError(
                REFRESH_FAILED,
                "Microsoft did not return an access token",
            )
        new_refresh = str(data.get("refresh_token") or "") or str(refresh_token)
        expires_at = _expiry_from_seconds(data.get("expires_in"))
        claimed_scopes = payload.get("scopes")
        if not isinstance(claimed_scopes, list):
            claimed_scopes = []
        new_payload: dict[str, Any] = {
            "access_token": access_token,
            "refresh_token": new_refresh,
            "client_id": client_id,
            "scopes": sorted(
                str(scope) for scope in claimed_scopes
            ) or sorted(MICROSOFT_WORKSPACE_OAUTH_SCOPES),
        }
        if expires_at is not None:
            new_payload["expires_at"] = expires_at.isoformat()
        try:
            new_version = future_version(credential_version)
        except ValueError:
            new_version = "v1"
        reference = encrypt_credential_reference(
            settings.encryption_key, new_payload, version=new_version
        )
        return CredentialRotationResult(
            credential_reference=reference,
            credential_version=new_version,
            expires_at=expires_at,
        )

    def disconnect(self) -> None:
        # Org-level disconnect is a local operation: the service layer
        # tombstones the reference. No provider-side cleanup to run.
        return None

    def revoke_token(self, refresh_token: str) -> None:
        # Microsoft provides no single-token revoke endpoint; see
        # ``MICROSOFT_REVOKE_NOOP`` in the module docstring.
        return None


def _token_error_code(response: Any) -> str:
    """Extract the v2.0 token endpoint ``error`` value from any response."""
    try:
        body = response.json()
    except ValueError:
        return str(response.status_code)
    if isinstance(body, dict):
        error = body.get("error")
        if isinstance(error, str):
            return error
        if isinstance(error, dict):
            return str(error.get("code") or error.get("message") or "")
        description = body.get("error_description")
        if isinstance(description, str):
            lowered = description.lower()
            for marker in ("invalid_grant", "consent_required", "admin_consent", "access_denied"):
                if marker in lowered:
                    return marker
    return str(response.status_code)


def _validate_scopes(token_data: dict[str, Any], requested: frozenset[str]) -> None:
    granted = {
        str(scope).strip()
        for scope in str(token_data.get("scope") or "").split()
        if str(scope).strip()
    }
    if granted and not granted <= requested:
        raise ProviderConnectionError(
            INSUFFICIENT_SCOPE,
            "Required permissions were not granted.",
        )
    if not requested.issubset(granted):
        raise ProviderConnectionError(
            INSUFFICIENT_SCOPE,
            "Required permissions were not granted.",
        )


def _decode_id_token(id_token: Any) -> dict[str, Any] | None:
    if not isinstance(id_token, str):
        return None
    try:
        segment = id_token.split(".")[1]
        padding = "=" * (-len(segment) % 4)
        raw = base64.urlsafe_b64decode(segment + padding).decode("utf-8")
        claims = json.loads(raw)
    except (IndexError, ValueError, TypeError, UnicodeDecodeError):
        return None
    return claims if isinstance(claims, dict) else None


def _token_payload(token_data: dict[str, Any], fallback_scopes: frozenset[str]) -> dict[str, Any]:
    expires_at = _expiry_from_seconds(token_data.get("expires_in"))
    scopes = sorted(
        str(scope)
        for scope in str(token_data.get("scope") or " ".join(sorted(fallback_scopes))).split()
        if str(scope).strip()
    )
    payload: dict[str, Any] = {
        "access_token": str(token_data.get("access_token") or ""),
        "refresh_token": str(token_data.get("refresh_token") or ""),
        "client_id": str(token_data.get("client_id") or settings.microsoft_client_id),
        "scopes": scopes,
    }
    if expires_at is not None:
        payload["expires_at"] = expires_at.isoformat()
    return payload


def _expiry_from_seconds(expires_in: Any) -> datetime | None:
    if isinstance(expires_in, (int, float)):
        return datetime.now(UTC) + timedelta(seconds=int(expires_in))
    if isinstance(expires_in, str):
        try:
            return datetime.now(UTC) + timedelta(seconds=int(expires_in))
        except ValueError:
            return None
    return None


def _domain_of(email: str) -> str | None:
    return email.partition("@")[2].strip() or None