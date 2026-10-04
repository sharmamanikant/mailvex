"""Zoho Mail sender provider (System B).

Real OAuth 2.0 + Zoho Mail REST adapter built on the Phase 10A foundation,
mirroring the Microsoft (Phase 10C) provider:

* OAuth tokens are stored only as an encrypted ``credential_reference``; the
  provider decrypts inside the provider boundary and never returns secrets.
* Minimum delegated scopes are requested: ``ZohoMail.messages.INSERT``
  (sending), ``ZohoMail.accounts.READ`` (account identity) and
  ``Aao.profile.Read`` (display identity). No mailbox-read / inbox scope is
  requested.
* ``validate_connection`` proves the account via Zoho userinfo before a
  connection is marked CONNECTED; the authenticated mailbox is never guessed.
* Sending targets ``/accounts/{accountId}/messages`` with an explicit
  ``fromAddress`` that Zoho validates against the authenticated account.
* A successful send response means Zoho *accepted* the message — it is NOT
  proof of final delivery. Callers receive the accepted status and must never
  interpret it as delivered.
* Errors are normalized onto :class:`ProviderErrorCode`; rate-limit responses
  preserve ``Retry-After`` for legitimate back-pressure handling.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime, timedelta
from typing import Any

import requests

from app.core.config import settings
from app.email_providers.base import (
    CredentialRotationResult,
    DiscoveryResult,
    EmailMessage,
    EmailProviderBase,
    EmailProviderError,
    ProviderCapabilities,
    ProviderConnectionConfig,
    ProviderErrorCode,
    ProviderSenderProfile,
    ValidationResult,
)
from app.email_providers.credentials import (
    decrypt_credential_reference,
    encrypt_credential_reference,
    future_version,
)

__all__ = ["ZOHO_SCOPES", "ZohoEmailProvider"]

# Minimum delegated scopes for the user-send flow: send mail + account read
# (identity) + profile read (display name). Inbox read is intentionally absent.
ZOHO_SCOPES: tuple[str, ...] = (
    "ZohoMail.messages.INSERT",
    "ZohoMail.accounts.READ",
    "Aao.profile.Read",
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class ZohoEmailProvider(EmailProviderBase):
    """Production Zoho Mail adapter (OAuth + REST sending)."""

    display_name = "Zoho Mail"

    def get_provider_name(self) -> str:
        return "ZOHO"

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name="ZOHO",
            display_name=self.display_name,
            connection_types=("OAUTH",),
            supports_oauth=True,
            supports_sender_discovery=True,
            supports_webhooks=False,
            supports_inbox_sync=False,
        )

    # ------------------------------------------------------------------ #
    # Local shape rules
    # ------------------------------------------------------------------ #
    def _validate_shape(self, config: ProviderConnectionConfig) -> tuple[bool, str]:
        if config.connection_type != "OAUTH":
            return False, "Zoho connections must use OAuth"
        if not config.external_account_id:
            return False, "A Zoho account id is required"
        return True, "Configuration looks valid"

    # ------------------------------------------------------------------ #
    # Token machinery (provider boundary only — never expose tokens)
    # ------------------------------------------------------------------ #
    @property
    def _authority(self) -> str:
        return (settings.zoho_authority or "https://accounts.zoho.com").rstrip("/")

    @property
    def _dc(self) -> str:
        host = self._authority.replace("https://", "").replace("http://", "").split("/")[0]
        parts = host.split(".")
        return parts[-1] if parts else "com"

    @property
    def _mail_base(self) -> str:
        return f"https://mail.zoho.{self._dc}/api"

    @staticmethod
    def _decrypt(config: ProviderConnectionConfig) -> dict[str, Any]:
        if not config.credential_reference:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_REQUIRED,
                "Credentials have not been stored for this connection",
            )
        try:
            payload = decrypt_credential_reference(settings.encryption_key, config.credential_reference)
        except ValueError:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_FAILED,
                "Stored credentials could not be decoded",
            ) from None
        return dict(payload)

    @staticmethod
    def _client_credentials() -> tuple[str, str]:
        if not settings.zoho_client_id or not settings.zoho_client_secret:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_REQUIRED,
                "Zoho OAuth is not configured",
            )
        return settings.zoho_client_id, settings.zoho_client_secret

    def _refresh_access_token(self, payload: dict[str, Any]) -> tuple[str, str, datetime | None]:
        """Exchange a stored refresh token for a fresh access token."""
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_REQUIRED,
                "This connection has no refresh token; reconnect required",
            )
        client_id, client_secret = self._client_credentials()
        try:
            response = requests.post(
                f"{self._authority}/oauth/v2/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": str(refresh_token),
                    "grant_type": "refresh_token",
                    "scope": " ".join(ZOHO_SCOPES),
                },
                timeout=15,
            )
        except requests.RequestException as exc:
            raise EmailProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "Zoho token refresh failed",
            ) from exc
        if response.status_code in (400, 401):
            raise EmailProviderError(
                ProviderErrorCode.AUTH_REQUIRED,
                "Zoho authorization expired; reconnect required",
            )
        if not response.ok:
            raise EmailProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "Zoho token endpoint is unavailable",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise EmailProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "Zoho returned an unreadable token response",
            ) from exc
        access_token = str(data.get("access_token") or "")
        if not access_token:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_FAILED,
                "Zoho did not return an access token",
            )
        new_refresh = str(data.get("refresh_token") or "") or str(refresh_token)
        expires_at: datetime | None = None
        expires_in = data.get("expires_in")
        if isinstance(expires_in, (int, float)):
            expires_at = datetime.now(UTC) + timedelta(seconds=int(expires_in))
        return access_token, new_refresh, expires_at

    def _token(self, config: ProviderConnectionConfig) -> dict[str, Any]:
        """Return a refreshed ``payload`` so API calls keep working."""
        payload = self._decrypt(config)
        access_token = str(payload.get("access_token") or "")
        expires_at = _parse_expiry(payload.get("expires_at"))
        if not access_token or (expires_at is not None and expires_at <= datetime.now(UTC)):
            access_token, new_refresh, new_expires = self._refresh_access_token(payload)
            payload = dict(payload)
            payload["access_token"] = access_token
            payload["refresh_token"] = new_refresh
            if new_expires is not None:
                payload["expires_at"] = new_expires.isoformat()
        return payload

    def _zoho(self, method: str, path: str, config: ProviderConnectionConfig, *, json: dict[str, Any] | None = None, params: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform an authenticated Zoho Mail API call, normalizing failures."""
        payload = self._token(config)
        access_token = str(payload["access_token"])
        try:
            response = requests.request(
                method,
                f"{self._mail_base}{path}",
                headers={
                    "Authorization": f"Zoho-oauthtoken {access_token}",
                    "Content-Type": "application/json",
                },
                json=json,
                params=params,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise EmailProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "Zoho Mail API is unreachable",
            ) from exc
        body: dict[str, Any] = {}
        if response.content:
            try:
                parsed = response.json()
                if isinstance(parsed, dict):
                    body = parsed
            except ValueError:
                body = {}
        if response.status_code == 401:
            raise EmailProviderError(ProviderErrorCode.AUTH_FAILED, "Zoho rejected the credentials")
        if response.status_code == 429:
            raise EmailProviderError(
                ProviderErrorCode.RATE_LIMITED,
                "Zoho rate limited the request",
                retry_after=_retry_after_from(response),
            )
        if response.status_code == 403:
            raise EmailProviderError(ProviderErrorCode.PERMISSION_DENIED, "Zoho denied access for this account")
        if response.status_code >= 500:
            raise EmailProviderError(ProviderErrorCode.PROVIDER_UNAVAILABLE, "Zoho Mail API is unavailable")
        if not response.ok:
            raise _zoho_error(response, body)
        status = body.get("status") or {}
        code = int(status.get("code") or 0) if isinstance(status, dict) else 0
        if code and code != 2000:
            raise _zoho_error(response, body)
        return body

    def _userinfo(self, config: ProviderConnectionConfig) -> dict[str, Any]:
        payload = self._token(config)
        access_token = str(payload["access_token"])
        try:
            response = requests.get(
                f"{self._authority}/oauth/v2/userinfo",
                headers={"Authorization": f"Zoho-oauthtoken {access_token}"},
                timeout=20,
            )
        except requests.RequestException as exc:
            raise EmailProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "Zoho identity could not be resolved",
            ) from exc
        if not response.ok:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_FAILED,
                "Zoho identity could not be resolved",
            )
        try:
            data = response.json()
        except ValueError:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_FAILED,
                "Zoho returned an unreadable identity response",
            ) from None
        if not isinstance(data, dict):
            raise EmailProviderError(ProviderErrorCode.AUTH_FAILED, "Zoho identity is unavailable")
        return data

    def _identity(self, config: ProviderConnectionConfig) -> tuple[str, str, str]:
        """Return ``(account_id, email, display_name)`` from userinfo."""
        info = self._userinfo(config)
        email = str(info.get("Email") or info.get("email") or "").strip().lower()
        account_id = str(info.get("ZUID") or info.get("zuid") or "").strip() or email
        display_name = str(info.get("Display_Name") or info.get("Full_Name") or info.get("display_name") or "").strip()
        return account_id, email, display_name

    def _accounts(self, config: ProviderConnectionConfig) -> list[dict[str, Any]]:
        body = self._zoho("GET", "/accounts", config)
        data = body.get("data")
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def _resolve_account_id(self, config: ProviderConnectionConfig) -> str:
        """Pick the Zoho Mail account id used for sending.

        Prefers an explicitly stored account id; otherwise matches the
        authenticated mailbox against the account list, falling back to the
        first available account (matching the connected identity).
        """
        metadata_id = str(config.metadata.get("zoho_account_id") or "").strip()
        if metadata_id:
            return metadata_id
        accounts = self._accounts(config)
        if not accounts:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_FAILED,
                "No Zoho Mail accounts were returned; reconnect required",
            )
        email = (config.email or "").lower()
        local_part = email.split("@", 1)[0].lower() if "@" in email else ""
        for account in accounts:
            name = str(account.get("accountName") or account.get("account_name") or "").lower()
            if local_part and local_part in name:
                return str(account.get("accountId") or account.get("account_id") or "")
        return str(accounts[0].get("accountId") or accounts[0].get("account_id") or "")

    # ------------------------------------------------------------------ #
    # Connection lifecycle (real validation)
    # ------------------------------------------------------------------ #
    def validate_connection(self, config: ProviderConnectionConfig) -> ValidationResult:
        shape_ok, shape_message = self._validate_shape(config)
        if not shape_ok:
            return ValidationResult(valid=False, message=shape_message)
        if not config.credential_reference:
            return ValidationResult(
                valid=False,
                message="Credentials have not been stored for this connection",
                error_code=ProviderErrorCode.AUTH_REQUIRED,
            )
        try:
            account_id, email, _ = self._identity(config)
        except EmailProviderError as exc:
            return ValidationResult(valid=False, message=str(exc.code.value), error_code=exc.code)
        if not account_id or not email:
            return ValidationResult(
                valid=False,
                message=str(ProviderErrorCode.AUTH_FAILED.value),
                error_code=ProviderErrorCode.AUTH_FAILED,
            )
        expected = str(config.external_account_id or "").lower()
        if expected and expected not in (account_id.lower(), email, str(config.email or "").lower()):
            return ValidationResult(
                valid=False,
                message=str(ProviderErrorCode.AUTH_FAILED.value),
                error_code=ProviderErrorCode.AUTH_FAILED,
            )
        if config.email and config.email.lower() != email:
            return ValidationResult(
                valid=False,
                message=str(ProviderErrorCode.AUTH_FAILED.value),
                error_code=ProviderErrorCode.AUTH_FAILED,
            )
        return ValidationResult(valid=True, message="Zoho connection verified")

    def connect(self, config: ProviderConnectionConfig) -> None:
        result = self.validate_connection(config)
        if not result.valid:
            raise EmailProviderError(result.error_code or ProviderErrorCode.AUTH_FAILED, result.message)

    def disconnect(self, config: ProviderConnectionConfig) -> None:
        return None

    def refresh_credentials(self, config: ProviderConnectionConfig) -> CredentialRotationResult:
        """Refresh the Zoho access token and re-encrypt under a bumped version."""
        payload = self._decrypt(config)
        access_token, new_refresh, expires_at = self._refresh_access_token(payload)
        new_payload: dict[str, Any] = {
            "access_token": access_token,
            "refresh_token": new_refresh,
            "client_id": str(payload.get("client_id") or settings.zoho_client_id),
            "scopes": sorted(str(scope) for scope in (payload.get("scopes") or ZOHO_SCOPES)),
        }
        if expires_at is not None:
            new_payload["expires_at"] = expires_at.isoformat()
        try:
            new_version = future_version(config.credential_version)
        except ValueError:
            new_version = "v1"
        reference = encrypt_credential_reference(
            settings.encryption_key,
            new_payload,
            version=new_version,
        )
        return CredentialRotationResult(
            credential_reference=reference,
            credential_version=new_version,
            expires_at=expires_at,
        )

    # ------------------------------------------------------------------ #
    # Sender discovery (authenticated mailbox only — no invented aliases)
    # ------------------------------------------------------------------ #
    def discover_senders(self, config: ProviderConnectionConfig) -> DiscoveryResult:
        try:
            account_id, email, display_name = self._identity(config)
        except EmailProviderError as exc:
            return DiscoveryResult(senders=(), errors=(str(exc.code.value),))
        if not account_id or not email:
            return DiscoveryResult(
                senders=(),
                errors=(str(ProviderErrorCode.AUTH_FAILED.value),),
            )
        try:
            account_id = self._resolve_account_id(config) or account_id
        except EmailProviderError:
            pass
        return DiscoveryResult(
            senders=(
                ProviderSenderProfile(
                    email=email,
                    display_name=display_name or None,
                    external_sender_id=account_id,
                    verified=True,
                ),
            )
        )

    def get_sender_profile(self, config: ProviderConnectionConfig, email: str) -> ProviderSenderProfile | None:
        result = self.discover_senders(config)
        for profile in result.senders:
            if profile.email == email.lower():
                return profile
        return None

    # ------------------------------------------------------------------ #
    # Sending — /accounts/{id}/messages, explicit recipients only.
    # ------------------------------------------------------------------ #
    def send_message(
        self,
        config: ProviderConnectionConfig,
        message: EmailMessage,
    ) -> str:
        _validate_recipients(message.to)
        _validate_recipients(message.cc)
        _validate_recipients(message.bcc)
        if not message.from_email or not _EMAIL_RE.match(message.from_email):
            raise EmailProviderError(ProviderErrorCode.INVALID_RECIPIENT, f"Invalid sender address: {message.from_email}")
        account_id = self._resolve_account_id(config)
        if not account_id:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_FAILED,
                "No Zoho Mail account is available for sending",
            )
        self._zoho("POST", f"/accounts/{account_id}/messages", config, json=_compose_zoho_message(message))
        # Zoho returns no message id; a success means Zoho accepted the message —
        # it does NOT confirm delivery (spec item 9).
        return f"accepted:{_stable_id(message)}"


def _validate_recipients(recipients: tuple[str, ...]) -> None:
    for recipient in recipients:
        if not recipient or not _EMAIL_RE.match(recipient):
            raise EmailProviderError(
                ProviderErrorCode.INVALID_RECIPIENT,
                f"Invalid recipient address: {recipient}",
            )


def _compose_zoho_message(message: EmailMessage) -> dict[str, Any]:
    body: dict[str, Any] = {
        "fromAddress": message.from_email,
        "toAddress": ",".join(message.to),
        "subject": message.subject,
    }
    if message.cc:
        body["ccAddress"] = ",".join(message.cc)
    if message.reply_to:
        body["replyTo"] = message.reply_to
    if message.html_body:
        body["content"] = message.html_body
        body["mailFormat"] = "html"
    else:
        body["content"] = message.text_body or ""
        body["mailFormat"] = "plaintext"
    return body


def _retry_after_from(response: requests.Response) -> int | None:
    try:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _zoho_error(response: requests.Response, body: dict[str, Any]) -> EmailProviderError:
    status = body.get("status") or {}
    if isinstance(status, dict):
        description = str(status.get("description") or status.get("msg") or "").lower()
    else:
        description = ""
    code = int(status.get("code") or 0) if isinstance(status, dict) else 0
    retry_after = _retry_after_from(response)
    lowered = f"{description} {body.get('message') or ''}".lower()
    if code in (2023, 2025, 2020) or "authentication" in lowered or "token" in lowered or ("invalid" in lowered and "address" not in lowered):
        return EmailProviderError(ProviderErrorCode.AUTH_FAILED, "Zoho rejected the credentials", retry_after=retry_after)
    if code in (2040, 2041, 2029, 2006) or "rate" in lowered or "throttl" in lowered or "limit" in lowered:
        return EmailProviderError(ProviderErrorCode.RATE_LIMITED, "Zoho rate limited the request", retry_after=retry_after)
    if "permission" in lowered or "forbidden" in lowered or "authorization" in lowered:
        return EmailProviderError(ProviderErrorCode.PERMISSION_DENIED, "Zoho denied the request", retry_after=retry_after)
    if "recipient" in lowered or "address" in lowered or "invalid" in lowered:
        return EmailProviderError(ProviderErrorCode.INVALID_RECIPIENT, "Zoho rejected a recipient address", retry_after=retry_after)
    if "quota" in lowered or "storage" in lowered:
        return EmailProviderError(ProviderErrorCode.QUOTA_EXCEEDED, "Zoho mailbox quota was exceeded", retry_after=retry_after)
    if "fromaddress" in lowered or "sender" in lowered:
        return EmailProviderError(ProviderErrorCode.MESSAGE_REJECTED, "Zoho rejected the sender address", retry_after=retry_after)
    return EmailProviderError(ProviderErrorCode.MESSAGE_REJECTED, "Zoho rejected the message", retry_after=retry_after)


def _parse_expiry(raw: Any) -> datetime | None:
    if raw is None:
        return None
    if isinstance(raw, datetime):
        return raw if raw.tzinfo is None else raw.astimezone(UTC).replace(tzinfo=None)
    if isinstance(raw, str):
        try:
            parsed = datetime.fromisoformat(raw.replace("Z", "+00:00"))
            return parsed if parsed.tzinfo is None else parsed.astimezone(UTC).replace(tzinfo=None)
        except ValueError:
            return None
    return None


def _stable_id(message: EmailMessage) -> str:
    import hashlib

    to = ",".join(sorted(message.to))
    digest = hashlib.sha256(f"{message.from_email}|{to}|{message.subject}".encode()).hexdigest()[:16]
    return digest