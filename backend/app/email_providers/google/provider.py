"""Google Workspace / Gmail sender integration (Phase 10B).

Real OAuth + Gmail API adapter built on the Phase 10A foundation:

* OAuth tokens are stored only as an encrypted ``credential_reference``
  through the secure credential store; the provider decrypts *inside the
  provider boundary* and never returns secrets.
* Fallback scopes are ``gmail.send`` plus ``openid`` / ``userinfo.email``
  (identity). ``gmail.readonly`` is requested ONLY for connections that opted
  into inbox access (``connection_metadata["inbox_access"]``) — send-only
  senders keep minimum scopes and can never read a mailbox.
* ``sync_inbox`` is scope-guarded: calling it on a connection without a
  granted ``gmail.readonly`` raises :class:`EmailProviderError` with
  ``PERMISSION_DENIED`` instead of attempting a read.
* Webhooks and delivery-event sync remain explicitly unimplemented (spec item
  26).
* Errors are normalized onto :class:`ProviderErrorCode`; rate-limit/quota
  responses preserve ``Retry-After`` for legitimate back-pressure handling.
"""

from __future__ import annotations

import base64
import re
import urllib.parse
from collections.abc import Sequence
from datetime import UTC, datetime
from email.message import EmailMessage as _GmailRawMessage
from typing import Any, cast

from google.auth.transport.requests import Request as GoogleRequest
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

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
    future_version,
)

__all__ = [
    "GMAIL_INBOX_OAUTH_SCOPES",
    "GMAIL_INBOX_SCOPE",
    "GMAIL_OAUTH_SCOPES",
    "GoogleEmailProvider",
]

# Fallback scope set for the user-send flow: send-only Gmail access plus
# identity. No mailbox/inbox scopes are ever requested unless the connection
# explicitly opted into inbox access (``metadata["inbox_access"]``).
GMAIL_OAUTH_SCOPES: tuple[str, ...] = (
    "openid",
    "https://www.googleapis.com/auth/userinfo.email",
    "https://www.googleapis.com/auth/gmail.send",
)

# Gmail inbox-read scope (opt-in). Requires a fresh consent round-trip; it is
# never included for send-only connections.
GMAIL_INBOX_SCOPE = "https://www.googleapis.com/auth/gmail.readonly"

# Opt-in extended set: base scopes + mailbox-read. Requested only when a
# connection has ``metadata["inbox_access"]`` set before authorization.
GMAIL_INBOX_OAUTH_SCOPES: tuple[str, ...] = (*GMAIL_OAUTH_SCOPES, GMAIL_INBOX_SCOPE)

_GMAIL_SCOPE_ALLOWLIST: frozenset[str] = frozenset(GMAIL_INBOX_OAUTH_SCOPES)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class GoogleEmailProvider(EmailProviderBase):
    """Production Google Workspace / Gmail adapter (Phase 10B)."""

    display_name = "Google Workspace / Gmail"

    def get_provider_name(self) -> str:
        return "GOOGLE"

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name="GOOGLE",
            display_name=self.display_name,
            connection_types=("OAUTH",),
            supports_oauth=True,
            supports_sender_discovery=True,
            supports_webhooks=False,
            supports_inbox_sync=True,
        )

    # ------------------------------------------------------------------ #
    # Local shape rules
    # ------------------------------------------------------------------ #
    def _validate_shape(self, config: ProviderConnectionConfig) -> tuple[bool, str]:
        if config.connection_type != "OAUTH":
            return False, "Google connections must use OAuth"
        if not config.external_account_id:
            return False, "A Google account id is required"
        return True, "Configuration looks valid"

    # ------------------------------------------------------------------ #
    # Private helpers (provider boundary only — never expose tokens)
    # ------------------------------------------------------------------ #
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

    def _build_service(self, config: ProviderConnectionConfig) -> Any:
        """Build an authenticated Gmail service, refreshing the access token.

        Refresh happens in-memory so API calls work even when the persisted
        access token has expired; durable rotation is exposed separately via
        :meth:`refresh_credentials` and the refresh-credentials endpoint.
        """
        payload = self._decrypt(config)
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_REQUIRED,
                "This connection has no refresh token; reconnect required",
            )
        credentials = Credentials(
            token=str(payload.get("access_token") or ""),
            refresh_token=str(refresh_token),
            token_uri=str(payload.get("token_uri") or "https://oauth2.googleapis.com/token"),
            client_id=str(payload.get("client_id") or settings.google_client_id),
            client_secret=str(settings.google_client_secret),
            scopes=list(payload.get("scopes") or GMAIL_OAUTH_SCOPES),
        )  # type: ignore[no-untyped-call]
        if credentials.expired:
            try:
                credentials.refresh(GoogleRequest())  # type: ignore[no-untyped-call]
            except Exception as exc:
                raise EmailProviderError(
                    ProviderErrorCode.AUTH_REQUIRED,
                    "Google authorization expired; reconnect required",
                ) from exc
        return build("gmail", "v1", credentials=credentials, cache_discovery=False)

    def _profile(self, config: ProviderConnectionConfig) -> dict[str, Any]:
        service = self._build_service(config)
        try:
            return cast(dict[str, Any], service.users().getProfile(userId="me").execute())
        except EmailProviderError:
            raise
        except HttpError as exc:
            raise self._map_http_error(exc) from exc

    def _expected_account(self, config: ProviderConnectionConfig) -> str:
        expected = str(config.metadata.get("google_sub") or config.external_account_id or "").strip().lower()
        if not expected:
            raise EmailProviderError(ProviderErrorCode.AUTH_FAILED, "Google account identity is unknown")
        return expected

    # ------------------------------------------------------------------ #
    # Connection lifecycle (real validation, never a real email send)
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
            profile = self._profile(config)
        except EmailProviderError as exc:
            return ValidationResult(
                valid=False,
                message=str(exc.code.value),
                error_code=exc.code,
            )
        resolved_email = str(profile.get("emailAddress") or "").strip().lower()
        if not resolved_email:
            return ValidationResult(
                valid=False,
                message=str(ProviderErrorCode.AUTH_FAILED.value),
                error_code=ProviderErrorCode.AUTH_FAILED,
            )
        expected = self._expected_account(config)
        if resolved_email not in (expected, str(config.external_account_id or "").lower()):
            return ValidationResult(
                valid=False,
                message=str(ProviderErrorCode.AUTH_FAILED.value),
                error_code=ProviderErrorCode.AUTH_FAILED,
            )
        if config.email and config.email.lower() != resolved_email:
            return ValidationResult(
                valid=False,
                message=str(ProviderErrorCode.AUTH_FAILED.value),
                error_code=ProviderErrorCode.AUTH_FAILED,
            )
        return ValidationResult(valid=True, message="Google connection verified")

    def connect(self, config: ProviderConnectionConfig) -> None:
        result = self.validate_connection(config)
        if not result.valid:
            raise EmailProviderError(
                result.error_code or ProviderErrorCode.AUTH_FAILED,
                result.message,
            )

    def disconnect(self, config: ProviderConnectionConfig) -> None:
        # OAuth disconnect is local (the connection row + revoked reference are
        # managed by the integration service).
        return None

    def refresh_credentials(self, config: ProviderConnectionConfig) -> CredentialRotationResult:
        """Refresh the Google access token and re-encrypt the credential
        reference under a bumped version (durable rotation)."""
        payload = self._decrypt(config)
        refresh_token = payload.get("refresh_token")
        if not refresh_token:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_REQUIRED,
                "This connection has no refresh token; reconnect required",
            )
        credentials = Credentials(
            token=str(payload.get("access_token") or ""),
            refresh_token=str(refresh_token),
            token_uri=str(payload.get("token_uri") or "https://oauth2.googleapis.com/token"),
            client_id=str(payload.get("client_id") or settings.google_client_id),
            client_secret=str(settings.google_client_secret),
            scopes=list(payload.get("scopes") or GMAIL_OAUTH_SCOPES),
        )  # type: ignore[no-untyped-call]
        try:
            credentials.refresh(GoogleRequest())  # type: ignore[no-untyped-call]
        except Exception as exc:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_REQUIRED,
                "Google authorization expired; reconnect required",
            ) from exc
        if not credentials.token:
            raise EmailProviderError(ProviderErrorCode.AUTH_FAILED, "Google did not return an access token")
        new_payload: dict[str, Any] = {
            "access_token": credentials.token,
            "refresh_token": credentials.refresh_token or refresh_token,
            "client_id": credentials.client_id or settings.google_client_id,
            "token_uri": credentials.token_uri or "https://oauth2.googleapis.com/token",
            "scopes": sorted(str(scope) for scope in (credentials.scopes or GMAIL_OAUTH_SCOPES)),
        }
        if credentials.expiry:
            new_payload["expires_at"] = credentials.expiry.isoformat()
        try:
            new_version = future_version(config.credential_version)
        except ValueError:
            new_version = "v1"
        from app.email_providers.credentials import encrypt_credential_reference

        reference = encrypt_credential_reference(
            settings.encryption_key,
            new_payload,
            version=new_version,
        )
        return CredentialRotationResult(
            credential_reference=reference,
            credential_version=new_version,
            expires_at=credentials.expiry,
        )

    # ------------------------------------------------------------------ #
    # Sender discovery (identity only — minimum scopes)
    # ------------------------------------------------------------------ #
    def discover_senders(self, config: ProviderConnectionConfig) -> DiscoveryResult:
        try:
            profile = self._profile(config)
        except EmailProviderError as exc:
            return DiscoveryResult(
                senders=(),
                errors=(str(exc.code.value),),
            )
        email = str(profile.get("emailAddress") or "").strip().lower()
        if not email:
            return DiscoveryResult(
                senders=(),
                errors=(str(ProviderErrorCode.AUTH_FAILED.value),),
            )
        profile_id = str(config.metadata.get("google_sub") or "").strip() or email
        return DiscoveryResult(
            senders=(
                ProviderSenderProfile(
                    email=email,
                    display_name=self.display_name,
                    external_sender_id=profile_id,
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
    # Sending (spec item 15/16) — Gmail API, single explicit recipient.
    # ------------------------------------------------------------------ #
    def send_message(
        self,
        config: ProviderConnectionConfig,
        message: EmailMessage,
    ) -> str:
        _validate_recipients(message.to)
        _validate_recipients(message.cc)
        _validate_recipients(message.bcc)
        service = self._build_service(config)
        raw = _compose_raw(message)
        try:
            result = service.users().messages().send(
                userId="me",
                body={"raw": raw},
            ).execute()
        except EmailProviderError:
            raise
        except HttpError as exc:
            raise self._map_http_error(exc) from exc
        message_id = str((result or {}).get("id") or "")
        if not message_id:
            raise EmailProviderError(
                ProviderErrorCode.UNKNOWN_PROVIDER_ERROR,
                "Google did not return a message id",
            )
        return message_id

    # ------------------------------------------------------------------ #
    # Inbox sync (opt-in ``gmail.readonly`` scope, spec item 26)
    # ------------------------------------------------------------------ #
    def sync_inbox(self, config: ProviderConnectionConfig, limit: int = 50) -> list[dict[str, object]]:
        """Pull recent inbox messages on the authenticated mailbox.

        Scope-guarded: without a granted ``gmail.readonly`` this raises
        ``PERMISSION_DENIED`` rather than attempting a mailbox read. Returns
        the normalized dict contract used by reply-sync linking.
        """
        if not _granted_scopes(config).issuperset({GMAIL_INBOX_SCOPE}):
            raise EmailProviderError(
                ProviderErrorCode.PERMISSION_DENIED,
                "Inbox access has not been granted for this connection; reconnect with inbox access",
            )
        service = self._build_service(config)
        try:
            listed = cast(
                dict[str, Any],
                service.users().messages().list(
                    userId="me", q="in:inbox", maxResults=limit + 1
                ).execute(),
            )
        except EmailProviderError:
            raise
        except HttpError as exc:
            raise self._map_http_error(exc) from exc
        entries: list[dict[str, Any]] = []
        for entry in listed.get("messages", []) or []:
            try:
                data = cast(
                    dict[str, Any],
                    service.users().messages().get(
                        userId="me", id=entry["id"], format="full"
                    ).execute(),
                )
            except EmailProviderError:
                raise
            except HttpError as exc:
                raise self._map_http_error(exc) from exc
            entries.append(data)
        sender = str(config.email or "").strip().lower()
        return [
            _parse_inbox_message(data, sender_email=sender)
            for data in entries[:limit]
        ]

    # ------------------------------------------------------------------ #
    # Error normalization (spec item 17) + provider-native scanning.
    # ------------------------------------------------------------------ #
    @staticmethod
    def _map_http_error(exc: HttpError) -> EmailProviderError:
        resp = cast(Any, getattr(exc, "resp", None))
        status = resp.status if resp is not None else 0
        retry_after = _retry_after_from(resp)
        reason = str(getattr(exc, "reason", "") or "").lower()
        if status == 401:
            return EmailProviderError(ProviderErrorCode.AUTH_FAILED, "Google rejected the credentials", retry_after=retry_after)
        if status in (400, 412, 413):
            if "rate" in reason or "quota" in reason or "limit" in reason:
                return EmailProviderError(ProviderErrorCode.QUOTA_EXCEEDED, "Google application/quota limit reached", retry_after=retry_after)
            return EmailProviderError(ProviderErrorCode.MESSAGE_REJECTED, "Google rejected the message", retry_after=retry_after)
        if status == 403:
            if "rate" in reason or "quota" in reason or "limit" in reason:
                return EmailProviderError(ProviderErrorCode.QUOTA_EXCEEDED, "Google sending quota exceeded", retry_after=retry_after)
            return EmailProviderError(ProviderErrorCode.PERMISSION_DENIED, "Google denied access to this account", retry_after=retry_after)
        if status == 429:
            return EmailProviderError(ProviderErrorCode.RATE_LIMITED, "Google rate limited the request", retry_after=retry_after)
        if status >= 500 or status == 0:
            return EmailProviderError(ProviderErrorCode.PROVIDER_UNAVAILABLE, "Google service is unavailable", retry_after=retry_after)
        return EmailProviderError(ProviderErrorCode.UNKNOWN_PROVIDER_ERROR, "Google request failed", retry_after=retry_after)


def _validate_recipients(recipients: Sequence[str]) -> None:
    for recipient in recipients:
        if not recipient or not _EMAIL_RE.match(recipient):
            raise EmailProviderError(
                ProviderErrorCode.INVALID_RECIPIENT,
                f"Invalid recipient address: {recipient}",
            )


def _retry_after_from(resp: Any) -> int | None:
    try:
        headers = getattr(resp, "headers", None)
        if not headers:
            return None
        value = headers.get("Retry-After") or headers.get("retry-after")
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _compose_raw(message: EmailMessage) -> str:
    """Compose an RFC 5322 message and return its URL-safe base64 payload."""
    raw = _GmailRawMessage()
    raw["From"] = message.from_email
    raw["To"] = ", ".join(message.to)
    if message.cc:
        raw["Cc"] = ", ".join(message.cc)
    if message.bcc:
        raw["Bcc"] = ", ".join(message.bcc)
    raw["Subject"] = message.subject
    if message.reply_to:
        raw["Reply-To"] = message.reply_to
    for name, value in message.headers.items():
        if name.lower() in {"from", "to", "cc", "bcc", "subject", "reply-to"}:
            continue
        raw[name] = value
    if message.text_body is not None:
        raw.set_content(message.text_body or "")
        if message.html_body:
            raw.add_alternative(message.html_body, subtype="html")
    elif message.html_body:
        raw.set_content(message.html_body, subtype="html")
    else:
        raw.set_content("")
    for attachment in message.attachments:
        maintype, _, subtype = attachment.mime_type.partition("/")
        try:
            raw.add_attachment(
                attachment.content,
                maintype=maintype or "application",
                subtype=subtype or "octet-stream",
                filename=attachment.filename,
            )
        except AttributeError:
            # Older Python email API fallback: attach with generic mime type.
            raw.add_attachment(attachment.content, subtype="octet-stream", filename=attachment.filename)
    return _urlsafe_base64(raw.as_bytes())


def _urlsafe_base64(payload: bytes) -> str:
    return base64.urlsafe_b64encode(payload).decode("ascii").rstrip("=")


def _granted_scopes(config: ProviderConnectionConfig) -> set[str]:
    raw = config.metadata.get("scopes")
    if not isinstance(raw, list):
        return set()
    return {str(scope).strip() for scope in raw if str(scope).strip()}


def _header(payload: dict[str, Any], name: str) -> str:
    lower = name.lower()
    for header in payload.get("headers", []) or []:
        if str(header.get("name", "")).lower() == lower:
            return str(header.get("value", ""))
    return ""


def _extract_text(payload: dict[str, Any]) -> str:
    body = payload.get("body") or {}
    if body.get("data"):
        return _decode(str(body["data"]))
    for part in payload.get("parts", []) or []:
        if part.get("mimeType") == "text/plain":
            text = _extract_text(part)
            if text:
                return text
        elif part.get("mimeType") == "text/html":
            nested = _extract_text(part)
            if nested:
                return nested
    return ""


def _decode(encoded: str) -> str:
    try:
        text = base64.urlsafe_b64decode(encoded).decode("utf-8", errors="replace")
    except (ValueError, TypeError):
        return ""
    return urllib.parse.unquote(text)


def _parse_inbox_message(data: dict[str, Any], *, sender_email: str) -> dict[str, object]:
    payload = data.get("payload") or {}
    from_addr = _header(payload, "From")
    internal = int(float(data.get("internalDate", "0") or 0))
    received = datetime.fromtimestamp(internal / 1000, tz=UTC) if internal else datetime.now(UTC)
    direction = "OUTBOUND" if sender_email and sender_email in from_addr.lower() else "INBOUND"
    return {
        "id": str(data.get("id", "")),
        "thread_id": str(data.get("threadId", "")),
        "message_id": _header(payload, "Message-Id"),
        "in_reply_to": _header(payload, "In-Reply-To"),
        "references": _header(payload, "References"),
        "subject": _header(payload, "Subject"),
        "from": from_addr,
        "received_at": received.isoformat(),
        "body_preview": _extract_text(payload)[:500],
        "direction": direction,
    }