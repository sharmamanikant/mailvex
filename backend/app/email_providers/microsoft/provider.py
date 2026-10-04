"""Microsoft 365 / Exchange Online sender integration (Phase 10C).

Real OAuth + Microsoft Graph adapter built on the Phase 10A foundation,
mirroring the Google (Phase 10B) provider:

* OAuth tokens are stored only as an encrypted ``credential_reference`` through
  the secure credential store; the provider decrypts *inside the provider
  boundary* and never returns secrets.
* Fallback delegated permissions are ``Mail.Send`` (sending) plus ``User.Read``
  and ``offline_access`` (identity + refresh). ``Mail.Read`` is requested ONLY
  for connections that opted into inbox access
  (``connection_metadata["inbox_access"]``) — send-only senders keep minimum
  permissions and can never read a mailbox.
* ``sync_inbox`` is scope-guarded: without a granted ``Mail.Read`` it raises
  :class:`EmailProviderError` with ``PERMISSION_DENIED`` instead of reading.
* Webhooks and delivery-event sync remain explicitly unimplemented (spec item
  26).
* We only ever send as the authenticated mailbox identity ("me"). Alias /
  Send-As identities are NOT invented: sender identity is provider-controlled,
  and we import strictly the authenticated mailbox.
* Errors are normalized onto :class:`ProviderErrorCode`; rate-limit/quota
  responses preserve ``Retry-After`` for legitimate back-pressure handling.
* A successful Graph ``/me/sendMail`` response means Microsoft *accepted* the
  message — it is NOT proof of final delivery. Callers receive the accepted
  status and must never interpret it as delivered.
"""

from __future__ import annotations

import re
from datetime import UTC, datetime
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

__all__ = ["GRAPH_INBOX_SCOPES", "GRAPH_SCOPES", "MAIL_READ_SCOPE", "MicrosoftEmailProvider"]

GRAPH_BASE = "https://graph.microsoft.com/v1.0"

# Fallback delegated permissions for the user-send flow (spec item 1):
# Mail.Send to send via the signed-in mailbox, User.Read for identity
# discovery, offline_access + openid/profile/email for refresh tokens and
# identity claims. No mailbox-wide Mail.Read is requested by default.
GRAPH_SCOPES: tuple[str, ...] = (
    "openid",
    "profile",
    "email",
    "offline_access",
    "User.Read",
    "Mail.Send",
)

# Mailbox-read permission (opt-in). Requires a fresh consent round-trip; it is
# never included for send-only connections.
MAIL_READ_SCOPE = "Mail.Read"

# Opt-in extended set: fallback permissions + mailbox-read.
GRAPH_INBOX_SCOPES: tuple[str, ...] = (*GRAPH_SCOPES, MAIL_READ_SCOPE)

_GRAPH_SCOPE_ALLOWLIST: frozenset[str] = frozenset(GRAPH_INBOX_SCOPES)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class MicrosoftEmailProvider(EmailProviderBase):
    """Production Microsoft 365 / Exchange Online adapter (Phase 10C)."""

    display_name = "Microsoft 365 / Exchange Online"

    def get_provider_name(self) -> str:
        return "MICROSOFT"

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name="MICROSOFT",
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
            return False, "Microsoft 365 connections must use OAuth"
        if not config.external_account_id:
            return False, "A Microsoft account id is required"
        return True, "Configuration looks valid"

    # ------------------------------------------------------------------ #
    # Token machinery (provider boundary only — never expose tokens)
    # ------------------------------------------------------------------ #
    @property
    def _authority(self) -> str:
        base = (settings.microsoft_authority or "https://login.microsoftonline.com").rstrip("/")
        tenant = settings.microsoft_tenant_id or "common"
        return f"{base}/{tenant}"

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
        if not settings.microsoft_client_id or not settings.microsoft_client_secret:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_REQUIRED,
                "Microsoft OAuth is not configured",
            )
        return settings.microsoft_client_id, settings.microsoft_client_secret

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
                f"{self._authority}/oauth2/v2.0/token",
                data={
                    "client_id": client_id,
                    "client_secret": client_secret,
                    "refresh_token": str(refresh_token),
                    "grant_type": "refresh_token",
                    "scope": " ".join(GRAPH_SCOPES),
                },
                timeout=15,
            )
        except requests.RequestException as exc:
            raise EmailProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "Microsoft token refresh failed",
            ) from exc
        if response.status_code in (400, 401):
            # Revoked consent / invalid refresh token -> stop retrying forever.
            raise EmailProviderError(
                ProviderErrorCode.AUTH_REQUIRED,
                "Microsoft authorization expired; reconnect required",
            )
        if not response.ok:
            raise EmailProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "Microsoft token endpoint is unavailable",
            )
        try:
            data = response.json()
        except ValueError as exc:
            raise EmailProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "Microsoft returned an unreadable token response",
            ) from exc
        access_token = str(data.get("access_token") or "")
        if not access_token:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_FAILED,
                "Microsoft did not return an access token",
            )
        new_refresh = str(data.get("refresh_token") or "") or str(refresh_token)
        expires_at: datetime | None = None
        expires_in = data.get("expires_in")
        if isinstance(expires_in, (int, float)):
            expires_at = datetime.now(UTC).replace(tzinfo=None) + __import__(
                "datetime", fromlist=["timedelta"]
            ).timedelta(seconds=int(expires_in))
        return access_token, new_refresh, expires_at

    def _token(self, config: ProviderConnectionConfig) -> dict[str, Any]:
        """Return ``(payload, access_token, refresh_token, expires_at)``,
        transparently refreshing in-memory when the stored access token is
        expired/missing so routine API calls keep working."""
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

    def _graph(self, method: str, path: str, config: ProviderConnectionConfig, *, json: dict[str, Any] | None = None) -> dict[str, Any]:
        """Perform an authenticated Microsoft Graph request."""
        payload = self._token(config)
        access_token = str(payload["access_token"])
        try:
            response = requests.request(
                method,
                f"{GRAPH_BASE}{path}",
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                json=json,
                timeout=20,
            )
        except requests.RequestException as exc:
            raise EmailProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "Microsoft Graph is unreachable",
            ) from exc
        if response.status_code == 401:
            raise EmailProviderError(ProviderErrorCode.AUTH_FAILED, "Microsoft rejected the credentials")
        if response.status_code == 403:
            return self._map_status(response)
        if response.status_code == 429:
            return self._map_status(response)
        if response.status_code >= 500:
            return self._map_status(response)
        if not response.ok:
            return self._map_status(response)
        if not response.content:
            return {}
        try:
            body = response.json()
        except ValueError:
            return {}
        if not isinstance(body, dict):
            return {}
        return body

    def _me(self, config: ProviderConnectionConfig) -> dict[str, Any]:
        return self._graph("GET", "/me?$select=id,mail,userPrincipalName,displayName", config)

    def _expected_account(self, config: ProviderConnectionConfig) -> str:
        expected = str(config.metadata.get("microsoft_id") or config.external_account_id or "").strip().lower()
        if not expected:
            raise EmailProviderError(ProviderErrorCode.AUTH_FAILED, "Microsoft account identity is unknown")
        return expected

    @staticmethod
    def _extract_email(profile: dict[str, Any]) -> str:
        email = str(profile.get("mail") or profile.get("userPrincipalName") or "").strip().lower()
        return email

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
            profile = self._me(config)
        except EmailProviderError as exc:
            return ValidationResult(valid=False, message=str(exc.code.value), error_code=exc.code)
        resolved_email = self._extract_email(profile)
        if not resolved_email:
            return ValidationResult(
                valid=False,
                message=str(ProviderErrorCode.AUTH_FAILED.value),
                error_code=ProviderErrorCode.AUTH_FAILED,
            )
        expected = self._expected_account(config)
        if expected not in (str(profile.get("id") or "").lower(), resolved_email, str(config.external_account_id or "").lower()):
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
        return ValidationResult(valid=True, message="Microsoft connection verified")

    def connect(self, config: ProviderConnectionConfig) -> None:
        result = self.validate_connection(config)
        if not result.valid:
            raise EmailProviderError(result.error_code or ProviderErrorCode.AUTH_FAILED, result.message)

    def disconnect(self, config: ProviderConnectionConfig) -> None:
        return None

    def refresh_credentials(self, config: ProviderConnectionConfig) -> CredentialRotationResult:
        """Refresh the Microsoft access token and re-encrypt the credential
        reference under a bumped version (durable rotation)."""
        payload = self._decrypt(config)
        access_token, new_refresh, expires_at = self._refresh_access_token(payload)
        new_payload: dict[str, Any] = {
            "access_token": access_token,
            "refresh_token": new_refresh,
            "client_id": str(payload.get("client_id") or settings.microsoft_client_id),
            "scopes": sorted(str(scope) for scope in (payload.get("scopes") or GRAPH_SCOPES)),
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
            profile = self._me(config)
        except EmailProviderError as exc:
            return DiscoveryResult(senders=(), errors=(str(exc.code.value),))
        email = self._extract_email(profile)
        if not email:
            return DiscoveryResult(
                senders=(),
                errors=(str(ProviderErrorCode.AUTH_FAILED.value),),
            )
        account_id = str(profile.get("id") or "").strip() or email
        display_name = str(profile.get("displayName") or "").strip() or self.display_name
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
    # Sending (spec item 9) — Graph /me/sendMail, explicit recipients.
    # ------------------------------------------------------------------ #
    def send_message(
        self,
        config: ProviderConnectionConfig,
        message: EmailMessage,
    ) -> str:
        _validate_recipients(message.to)
        _validate_recipients(message.cc)
        _validate_recipients(message.bcc)
        payload = _compose_graph_message(message)
        self._graph("POST", "/me/sendMail", config, json=payload)
        # Graph returns 202 Accepted with no message id. A successful call means
        # Microsoft accepted the message — it does NOT mean it was delivered
        # (spec item 9). We surface a stable "accepted by provider" handle.
        return f"accepted:{_stable_id(message)}"

    # ------------------------------------------------------------------ #
    # Inbox sync (opt-in ``Mail.Read`` scope, spec item 26)
    # ------------------------------------------------------------------ #
    def sync_inbox(self, config: ProviderConnectionConfig, limit: int = 50) -> list[dict[str, object]]:
        """Pull recent inbox-folder messages on the authenticated mailbox.

        Scope-guarded: without a granted ``Mail.Read`` this raises
        ``PERMISSION_DENIED`` rather than attempting a mailbox read. Returns
        the normalized dict contract used by reply-sync linking.

        Reply linking is best-effort for Graph: ``sendMail`` returns no message
        id and Microsoft generates its own ``InternetMessageId`` for the sent
        item, so a reply's ``In-Reply-To`` cannot be pre-correlated with the
        ``accepted:`` handle persisted at send time without reading Sent Items
        (out of scope). The full threading metadata is still returned so
        linking works whenever the stored id does match.
        """
        if not _granted_scopes(config).issuperset({MAIL_READ_SCOPE}):
            raise EmailProviderError(
                ProviderErrorCode.PERMISSION_DENIED,
                "Inbox access has not been granted for this connection; reconnect with inbox access",
            )
        payload = self._token(config)
        access_token = str(payload["access_token"])
        try:
            response = requests.get(
                f"{GRAPH_BASE}/me/mailFolders/inbox/messages",
                params={
                    "$top": str(limit),
                    "$orderby": "receivedDateTime desc",
                    "$select": (
                        "id,conversationId,subject,from,receivedDateTime,"
                        "bodyPreview,internetMessageId,internetMessageHeaders"
                    ),
                },
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Content-Type": "application/json",
                },
                timeout=20,
            )
        except requests.RequestException as exc:
            raise EmailProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "Microsoft Graph is unreachable",
            ) from exc
        if not response.ok:
            self._map_status(response)
        sender = str(config.email or "").strip().lower()
        return [
            _parse_inbox_message(item, sender_email=sender)
            for item in (response.json().get("value") or [])
            if isinstance(item, dict)
        ]

    # ------------------------------------------------------------------ #
    # Error normalization (spec item 10)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _map_status(response: Any) -> dict[str, Any]:
        status = int(getattr(response, "status_code", 0))
        retry_after = _retry_after_from(response)
        body: dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                body = parsed
        except ValueError:
            pass
        error = body.get("error") or {}
        message = str(error.get("message") or body.get("message") or "").lower()
        code = str(error.get("code") or "").lower()

        if status == 401:
            raise EmailProviderError(ProviderErrorCode.AUTH_FAILED, "Microsoft rejected the credentials", retry_after=retry_after)
        if status == 429:
            raise EmailProviderError(ProviderErrorCode.RATE_LIMITED, "Microsoft rate limited the request", retry_after=retry_after)
        if status == 403:
            if "insufficient" in message or "permission" in code or "authorization" in code:
                raise EmailProviderError(ProviderErrorCode.PERMISSION_DENIED, "Microsoft denied access for this account", retry_after=retry_after)
            if "quota" in message or "limit" in message or "recipient" in message:
                raise EmailProviderError(ProviderErrorCode.QUOTA_EXCEEDED, "Microsoft sending quota or recipient limit reached", retry_after=retry_after)
            raise EmailProviderError(ProviderErrorCode.PERMISSION_DENIED, "Microsoft denied the request", retry_after=retry_after)
        if status == 404:
            raise EmailProviderError(ProviderErrorCode.AUTH_FAILED, "Microsoft account not found", retry_after=retry_after)
        if status == 409:
            raise EmailProviderError(ProviderErrorCode.MESSAGE_REJECTED, "Microsoft rejected the message", retry_after=retry_after)
        if status == 400:
            if "recipient" in message or "smtpaddress" in message or "invalid" in code:
                raise EmailProviderError(ProviderErrorCode.INVALID_RECIPIENT, "Microsoft rejected a recipient address", retry_after=retry_after)
            if "quota" in message or "limit" in message:
                raise EmailProviderError(ProviderErrorCode.QUOTA_EXCEEDED, "Microsoft sending quota reached", retry_after=retry_after)
            raise EmailProviderError(ProviderErrorCode.MESSAGE_REJECTED, "Microsoft rejected the message", retry_after=retry_after)
        if 500 <= status < 600:
            raise EmailProviderError(ProviderErrorCode.PROVIDER_UNAVAILABLE, "Microsoft Graph is unavailable", retry_after=retry_after)
        raise EmailProviderError(ProviderErrorCode.UNKNOWN_PROVIDER_ERROR, "Microsoft request failed", retry_after=retry_after)


def _validate_recipients(recipients: tuple[str, ...]) -> None:
    for recipient in recipients:
        if not recipient or not _EMAIL_RE.match(recipient):
            raise EmailProviderError(
                ProviderErrorCode.INVALID_RECIPIENT,
                f"Invalid recipient address: {recipient}",
            )


def _retry_after_from(response: Any) -> int | None:
    if response is None:
        return None
    try:
        headers = getattr(response, "headers", None)
        if not headers:
            return None
        value = headers.get("Retry-After") or headers.get("retry-after")
        if value is None:
            return None
        return int(value)
    except (TypeError, ValueError):
        return None


def _recipient_list(recipients: tuple[str, ...]) -> list[dict[str, Any]]:
    return [{"emailAddress": {"address": address}} for address in recipients]


def _compose_graph_message(message: EmailMessage) -> dict[str, Any]:
    """Convert the normalized EmailMessage into the Graph sendMail payload."""
    body_parts: dict[str, Any] = {}
    if message.html_body:
        body_parts["contentType"] = "HTML"
        body_parts["content"] = message.html_body
    elif message.text_body is not None:
        body_parts["contentType"] = "Text"
        body_parts["content"] = message.text_body or ""
    else:
        body_parts["contentType"] = "Text"
        body_parts["content"] = ""
    graph: dict[str, Any] = {
        "message": {
            "subject": message.subject,
            "body": body_parts,
            "toRecipients": _recipient_list(message.to),
        },
        "saveToSentItems": True,
    }
    msg = graph["message"]
    if message.cc:
        msg["ccRecipients"] = _recipient_list(message.cc)
    if message.bcc:
        msg["bccRecipients"] = _recipient_list(message.bcc)
    if message.reply_to:
        msg["replyTo"] = [{"emailAddress": {"address": message.reply_to}}]
    if message.attachments:
        msg["attachments"] = [
            {
                "@odata.type": "#microsoft.graph.fileAttachment",
                "name": attachment.filename,
                "contentBytes": __import__("base64", fromlist=["b64encode"]).b64encode(attachment.content).decode("ascii"),
                "contentType": attachment.mime_type,
            }
            for attachment in message.attachments
        ]
    return graph


def _stable_id(message: EmailMessage) -> str:
    import hashlib

    to = ",".join(sorted(message.to))
    digest = hashlib.sha256(f"{message.from_email}|{to}|{message.subject}".encode()).hexdigest()[:16]
    return digest


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


def _granted_scopes(config: ProviderConnectionConfig) -> set[str]:
    raw = config.metadata.get("scopes")
    if not isinstance(raw, list):
        return set()
    return {str(scope).strip() for scope in raw if str(scope).strip()}


def _parse_inbox_message(data: dict[str, Any], *, sender_email: str) -> dict[str, object]:
    headers: dict[str, str] = {}
    for header in data.get("internetMessageHeaders") or []:
        name = str(header.get("name") or "").strip().lower()
        if name:
            headers[name] = str(header.get("value") or "")
    from_addr = str(
        ((data.get("from") or {}).get("emailAddress") or {}).get("address") or ""
    )
    received_raw = data.get("receivedDateTime")
    if received_raw:
        try:
            received = datetime.fromisoformat(str(received_raw).replace("Z", "+00:00"))
        except ValueError:
            received = datetime.now(UTC)
    else:
        received = datetime.now(UTC)
    direction = "OUTBOUND" if sender_email and sender_email in from_addr.lower() else "INBOUND"
    return {
        "id": str(data.get("id", "")),
        "thread_id": str(data.get("conversationId", "")),
        "message_id": str(data.get("internetMessageId") or "") or headers.get("message-id", ""),
        "in_reply_to": headers.get("in-reply-to", ""),
        "references": headers.get("references", ""),
        "subject": str(data.get("subject", "")),
        "from": from_addr,
        "received_at": received.isoformat(),
        "body_preview": str(data.get("bodyPreview", ""))[:500],
        "direction": direction,
    }
