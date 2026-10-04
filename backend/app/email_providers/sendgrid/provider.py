"""SendGrid sender provider (System B).

Real REST adapter built on the Phase 10A foundation:

* The API key is read ONLY from the decrypted credential reference inside the
  provider boundary; it never appears in responses, audit records or logs.
* ``validate_connection`` proves the key works with a lightweight authenticated
  call to the SendGrid user profile endpoint before the connection is marked
  CONNECTED.
* Messages are sent through the v3 ``/mail/send`` endpoint. A 202 Accepted means
  SendGrid accepted the message for delivery — it is NOT proof of delivery.
* Errors are normalized onto :class:`ProviderErrorCode``; HTTP ``429``
  responses preserve ``Retry-After`` for legitimate back-pressure handling.
"""

from __future__ import annotations

import re
from typing import Any, NoReturn

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
from app.email_providers.credentials import decrypt_credential_reference

__all__ = ["SendGridEmailProvider"]

SENDGRID_BASE = "https://api.sendgrid.com/v3"

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SendGridEmailProvider(EmailProviderBase):
    """SendGrid Mail API adapter using a stored API key."""

    display_name = "SendGrid"

    def get_provider_name(self) -> str:
        return "SENDGRID"

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name="SENDGRID",
            display_name=self.display_name,
            connection_types=("API_KEY",),
            supports_api_key=True,
            supports_sender_discovery=False,
            supports_webhooks=True,
            supports_inbox_sync=False,
        )

    # ------------------------------------------------------------------ #
    # Local shape rules
    # ------------------------------------------------------------------ #
    def _validate_shape(self, config: ProviderConnectionConfig) -> tuple[bool, str]:
        if config.connection_type != "API_KEY":
            return False, "SendGrid connections use an API key"
        return True, "Configuration looks valid"

    # ------------------------------------------------------------------ #
    # Credential + client machinery (provider boundary only)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _decrypt(config: ProviderConnectionConfig) -> dict[str, Any]:
        if not config.credential_reference:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_REQUIRED,
                "SendGrid credentials have not been stored for this connection",
            )
        try:
            payload = decrypt_credential_reference(settings.encryption_key, config.credential_reference)
        except ValueError:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_FAILED,
                "Stored SendGrid credentials could not be decoded",
            ) from None
        return dict(payload)

    def _api_key(self, config: ProviderConnectionConfig) -> str:
        payload = self._decrypt(config)
        api_key = str(payload.get("api_key") or "").strip()
        if not api_key:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_REQUIRED,
                "No SendGrid API key is stored for this connection",
            )
        return api_key

    def _request(self, method: str, path: str, config: ProviderConnectionConfig, *, json: dict[str, Any] | None = None, params: dict[str, str] | None = None) -> requests.Response:
        api_key = self._api_key(config)
        try:
            return requests.request(
                method,
                f"{SENDGRID_BASE}{path}",
                headers={
                    "Authorization": f"Bearer {api_key}",
                    "Content-Type": "application/json",
                },
                json=json,
                params=params,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise EmailProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "SendGrid API is unreachable",
            ) from exc

    # ------------------------------------------------------------------ #
    # Connection lifecycle (real validation against the API)
    # ------------------------------------------------------------------ #
    def validate_connection(self, config: ProviderConnectionConfig) -> ValidationResult:
        shape_ok, shape_message = self._validate_shape(config)
        if not shape_ok:
            return ValidationResult(valid=False, message=shape_message)
        if not config.credential_reference:
            return ValidationResult(
                valid=False,
                message="SendGrid credentials have not been stored for this connection",
                error_code=ProviderErrorCode.AUTH_REQUIRED,
            )
        try:
            response = self._request("GET", "/user/profile", config)
        except EmailProviderError as exc:
            return ValidationResult(
                valid=False,
                message=str(exc.code.value),
                error_code=exc.code,
            )
        status = int(response.status_code)
        if status == 200:
            return ValidationResult(valid=True, message="SendGrid connection verified")
        if status == 401:
            return ValidationResult(
                valid=False,
                message=str(ProviderErrorCode.AUTH_FAILED.value),
                error_code=ProviderErrorCode.AUTH_FAILED,
            )
        if status == 429:
            return ValidationResult(
                valid=False,
                message=str(ProviderErrorCode.RATE_LIMITED.value),
                error_code=ProviderErrorCode.RATE_LIMITED,
            )
        return ValidationResult(
            valid=False,
            message=str(ProviderErrorCode.UNKNOWN_PROVIDER_ERROR.value),
            error_code=ProviderErrorCode.UNKNOWN_PROVIDER_ERROR,
        )

    def connect(self, config: ProviderConnectionConfig) -> None:
        result = self.validate_connection(config)
        if not result.valid:
            raise EmailProviderError(result.error_code or ProviderErrorCode.AUTH_FAILED, result.message)

    def disconnect(self, config: ProviderConnectionConfig) -> None:
        return None

    def refresh_credentials(self, config: ProviderConnectionConfig) -> CredentialRotationResult:
        raise EmailProviderError(
            ProviderErrorCode.AUTH_REQUIRED,
            "SendGrid API keys are static; upload a new key to rotate",
        )

    # ------------------------------------------------------------------ #
    # Sending (v3 /mail/send — explicit recipients only)
    # ------------------------------------------------------------------ #
    def send_message(
        self,
        config: ProviderConnectionConfig,
        message: EmailMessage,
    ) -> str:
        _validate_recipients(message.from_email)
        _validate_recipients(*message.to)
        _validate_recipients(*message.cc)
        _validate_recipients(*message.bcc)
        payload = _compose_mail_send(message)
        response = self._request("POST", "/mail/send", config, json=payload)
        status = int(response.status_code)
        if response.ok:
            # SendGrid returns 202 Accepted; the message id appears in the
            # per-recipient response headers, which we do not fetch. A success
            # means SendGrid accepted it — never treat as delivered.
            return f"accepted:{_stable_id(message)}"
        _raise_from_response(response, status)

    # ------------------------------------------------------------------ #
    # Discovery / inbox (not supported)
    # ------------------------------------------------------------------ #
    def discover_senders(self, config: ProviderConnectionConfig) -> DiscoveryResult:
        return DiscoveryResult(senders=(), errors=("SendGrid does not support sender discovery",))

    def get_sender_profile(self, config: ProviderConnectionConfig, email: str) -> ProviderSenderProfile | None:
        if config.email and config.email.lower() == email.lower():
            return ProviderSenderProfile(email=config.email, verified=False)
        return None


def _validate_recipients(*recipients: str) -> None:
    for recipient in recipients:
        if not recipient or not _EMAIL_RE.match(recipient):
            raise EmailProviderError(
                ProviderErrorCode.INVALID_RECIPIENT,
                f"Invalid recipient address: {recipient}",
            )


def _compose_mail_send(message: EmailMessage) -> dict[str, Any]:
    """Convert the normalized EmailMessage into the v3 /mail/send payload."""
    personalization: dict[str, Any] = {
        "to": [{"email": address} for address in message.to],
    }
    if message.cc:
        personalization["cc"] = [{"email": address} for address in message.cc]
    if message.bcc:
        personalization["bcc"] = [{"email": address} for address in message.bcc]
    content: list[dict[str, str]] = []
    if message.text_body is not None:
        content.append({"type": "text/plain", "value": message.text_body})
    if message.html_body:
        content.append({"type": "text/html", "value": message.html_body})
    if not content:
        content.append({"type": "text/plain", "value": ""})
    body: dict[str, Any] = {
        "personalizations": [personalization],
        "from": {"email": message.from_email},
        "subject": message.subject,
        "content": content,
    }
    if message.reply_to:
        body["reply_to"] = {"email": message.reply_to}
    if message.headers:
        body["headers"] = dict(message.headers)
    if message.attachments:
        import base64

        body["attachments"] = [
            {
                "content": base64.b64encode(attachment.content).decode("ascii"),
                "type": attachment.mime_type or "application/octet-stream",
                "filename": attachment.filename,
            }
            for attachment in message.attachments
        ]
    return body


def _retry_after_from(response: requests.Response) -> int | None:
    try:
        value = response.headers.get("Retry-After")
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _raise_from_response(response: requests.Response, status: int) -> NoReturn:
    retry_after = _retry_after_from(response)
    message = ""
    errors: list[Any] = []
    try:
        body = response.json()
        if isinstance(body, dict):
            message = str(body.get("errors") or body.get("message") or "")
            if isinstance(body.get("errors"), list):
                errors = body["errors"]
        elif isinstance(body, list):
            errors = body
    except ValueError:
        body = {}
    error = " ".join(
        str(item.get("message", "")) for item in errors if isinstance(item, dict)
    ).lower() or str(message).lower()
    if status == 401:
        raise EmailProviderError(ProviderErrorCode.AUTH_FAILED, "SendGrid rejected the API key", retry_after=retry_after)
    if status == 403:
        if "unverified" in error or "identity" in error:
            raise EmailProviderError(ProviderErrorCode.PERMISSION_DENIED, "SendGrid from-address is not a verified sender", retry_after=retry_after)
        raise EmailProviderError(ProviderErrorCode.PERMISSION_DENIED, "SendGrid denied the request", retry_after=retry_after)
    if status == 429:
        raise EmailProviderError(ProviderErrorCode.RATE_LIMITED, "SendGrid rate limited the request", retry_after=retry_after)
    if status == 400:
        if "recipient" in error:
            raise EmailProviderError(ProviderErrorCode.INVALID_RECIPIENT, "SendGrid rejected a recipient address", retry_after=retry_after)
        if "suppression" in error or "blocked" in error or "limit" in error:
            raise EmailProviderError(ProviderErrorCode.QUOTA_EXCEEDED, "SendGrid rejected the message due to suppression or limits", retry_after=retry_after)
        raise EmailProviderError(ProviderErrorCode.MESSAGE_REJECTED, "SendGrid rejected the message", retry_after=retry_after)
    if 500 <= status < 600:
        raise EmailProviderError(ProviderErrorCode.PROVIDER_UNAVAILABLE, "SendGrid API is unavailable", retry_after=retry_after)
    raise EmailProviderError(ProviderErrorCode.UNKNOWN_PROVIDER_ERROR, "SendGrid request failed", retry_after=retry_after)


def _stable_id(message: EmailMessage) -> str:
    import hashlib

    to = ",".join(sorted(message.to))
    digest = hashlib.sha256(f"{message.from_email}|{to}|{message.subject}".encode()).hexdigest()[:16]
    return digest