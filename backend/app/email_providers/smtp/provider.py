"""Generic SMTP sender provider (System B).

Real ``smtplib`` transport implemented on the Phase 10A foundation:

* Host/port/username come from ``connection_metadata``; the password is read
  ONLY from the decrypted credential reference inside the provider boundary.
* Every destination passes through ``app.security.smtp_policy`` before a socket
  is opened: private/loopback/metadata address resolution is rejected, denylisted
  ports are rejected, and authentication over cleartext is rejected unless the
  deployment explicitly opts out (SSRF + transport hardening).
* TLS is required whenever credentials are present: ``STARTTLS`` (587) or
  ``SSL`` (465) based on the configured ``smtp_security`` mode. A missing/blank
  mode falls back to STARTTLS (implicit TLS on 465).
* ``validate_connection`` performs a real SMTP handshake (and ``AUTH LOGIN``
  when credentials are stored) so a connection is only marked CONNECTED after
  the mailbox can actually be reached and authenticated.
* SMTP failures are normalized onto :class:`ProviderErrorCode`; ``421`` (SMTP
  service unavailable, typically throttle/policy) surfaces as ``RATE_LIMITED``.
"""

from __future__ import annotations

import re
import smtplib
import socket
import ssl
from email.message import EmailMessage as RFCMessage
from email.utils import formatdate, make_msgid
from typing import Any

from app.core.config import settings
from app.email_providers.base import (
    CredentialRotationResult,
    DiscoveryResult,
    EmailMessage,
    EmailProviderBase,
    EmailProviderError,
    ProviderCapabilities,
    ProviderConnectionConfig,
    ProviderConnectionRequirementError,
    ProviderErrorCode,
    ProviderSenderProfile,
    ValidationResult,
)
from app.email_providers.credentials import decrypt_credential_reference
from app.security.smtp_policy import SMTPPolicyViolation, validate_destination

__all__ = ["SMTPEmailProvider"]

_SMTP_HOST_KEY = "smtp_host"
_SMTP_PORT_KEY = "smtp_port"
_SMTP_USERNAME_KEY = "smtp_username"
_SMTP_SECURITY_KEY = "smtp_security"

SMTP_SECURITY_MODES = frozenset({"STARTTLS", "SSL", "TLS"})

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


class SMTPEmailProvider(EmailProviderBase):
    """Generic SMTP transport adapter with policy-enforced destination checks."""

    display_name = "Generic SMTP"

    def get_provider_name(self) -> str:
        return "SMTP"

    def get_capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(
            provider_name="SMTP",
            display_name=self.display_name,
            connection_types=("SMTP",),
            supports_smtp=True,
            supports_sender_discovery=False,
            supports_webhooks=False,
            supports_inbox_sync=False,
        )

    # ------------------------------------------------------------------ #
    # Local shape rules
    # ------------------------------------------------------------------ #
    def _validate_shape(self, config: ProviderConnectionConfig) -> tuple[bool, str]:
        if config.connection_type != "SMTP":
            return False, "SMTP connections use server credentials"
        if not config.email or "@" not in config.email:
            return False, "An envelope from-address (email) is required"
        host = str(config.metadata.get(_SMTP_HOST_KEY) or "").strip()
        port = config.metadata.get(_SMTP_PORT_KEY)
        if not host:
            return False, "SMTP host is required"
        try:
            port_value = int(str(port).strip())
        except (TypeError, ValueError):
            return False, "SMTP port must be a number"
        if not (1 <= port_value <= 65535):
            return False, "SMTP port must be between 1 and 65535"
        security = str(config.metadata.get(_SMTP_SECURITY_KEY) or "").upper()
        if security and security not in SMTP_SECURITY_MODES:
            return False, "SMTP security mode must be STARTTLS, SSL, or TLS"
        return True, "Configuration looks valid"

    # ------------------------------------------------------------------ #
    # Credential + connection details (provider boundary only)
    # ------------------------------------------------------------------ #
    @staticmethod
    def _decrypt(config: ProviderConnectionConfig) -> dict[str, Any]:
        if not config.credential_reference:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_REQUIRED,
                "SMTP credentials have not been stored for this connection",
            )
        try:
            payload = decrypt_credential_reference(settings.encryption_key, config.credential_reference)
        except ValueError:
            raise EmailProviderError(
                ProviderErrorCode.AUTH_FAILED,
                "Stored SMTP credentials could not be decoded",
            ) from None
        return dict(payload)

    def _connection_settings(self, config: ProviderConnectionConfig) -> tuple[str, int, str, str | None, str | None]:
        """Resolve ``(host, port, security, username, password)`` for a config.

        Credentials are coalesced from the decrypted reference (authoritative)
        with the non-secret smtp_username metadata as a fallback.
        """
        shape_ok, message = self._validate_shape(config)
        if not shape_ok:
            raise ProviderConnectionRequirementError(message)
        metadata = config.metadata
        host = str(metadata[_SMTP_HOST_KEY]).strip()
        port = int(str(metadata[_SMTP_PORT_KEY]).strip())  # validated by _validate_shape
        raw_security = str(metadata.get(_SMTP_SECURITY_KEY) or "").upper()
        if not raw_security:
            raw_security = "SSL" if port == 465 else "STARTTLS"
        security = "SSL" if raw_security == "SSL" else "STARTTLS"
        username = str(metadata.get(_SMTP_USERNAME_KEY) or "").strip() or None
        password: str | None = None
        try:
            payload = self._decrypt(config)
        except EmailProviderError:
            payload = {}
        if payload:
            username = str(payload.get("smtp_username") or username or "").strip() or None
            password = str(payload.get("smtp_password") or "").strip() or None
        return host, port, security, username, password

    def _validate_destination(self, config: ProviderConnectionConfig) -> None:
        """Enforce the SMTP security policy; raises ``SMTPPolicyViolation``."""
        shape_ok, _ = self._validate_shape(config)
        if not shape_ok:
            return
        host, port, security, username, password = self._connection_settings(config)
        validate_destination(
            host,
            port=port,
            tls_mode=security,
            username=username,
            password=password,
            allow_private_hosts=settings.smtp_allow_private_hosts,
            allow_plaintext_auth=settings.smtp_allow_plaintext_auth,
            allow_insecure_ports=settings.smtp_allow_insecure_ports,
        )

    # ------------------------------------------------------------------ #
    # Transport
    # ------------------------------------------------------------------ #
    def _connect(self, host: str, port: int, security: str, timeout: float = 20.0) -> Any:
        """Open an SMTP connection, upgrading to TLS when required."""
        client: smtplib.SMTP
        if security == "SSL":
            client = smtplib.SMTP_SSL(host, port, timeout=timeout, context=ssl.create_default_context())
        else:
            client = smtplib.SMTP(host, port, timeout=timeout)
            client.ehlo()
            if security == "STARTTLS":
                client.starttls(context=ssl.create_default_context())
                client.ehlo()
        return client

    # ------------------------------------------------------------------ #
    # Error normalization
    # ------------------------------------------------------------------ #
    @staticmethod
    def _map_exception(exc: BaseException) -> EmailProviderError:
        if isinstance(exc, SMTPPolicyViolation):
            return EmailProviderError(ProviderErrorCode.PERMISSION_DENIED, str(exc))
        if isinstance(exc, smtplib.SMTPAuthenticationError):
            return EmailProviderError(
                ProviderErrorCode.AUTH_FAILED,
                "SMTP authentication failed for this mailbox",
            )
        if isinstance(exc, smtplib.SMTPRecipientsRefused):
            return EmailProviderError(
                ProviderErrorCode.INVALID_RECIPIENT,
                "The SMTP server refused one or more recipients",
            )
        if isinstance(exc, smtplib.SMTPSenderRefused):
            return EmailProviderError(
                ProviderErrorCode.MESSAGE_REJECTED,
                "The SMTP server refused the sender envelope",
            )
        if isinstance(exc, smtplib.SMTPDataError):
            return EmailProviderError(
                ProviderErrorCode.MESSAGE_REJECTED,
                "The SMTP server rejected the message content",
            )
        if isinstance(exc, smtplib.SMTPResponseException) and exc.smtp_code == 421:
            return EmailProviderError(
                ProviderErrorCode.RATE_LIMITED,
                "The SMTP server is temporarily unavailable (possible rate limit)",
            )
        if isinstance(exc, smtplib.SMTPResponseException) and exc.smtp_code in (450, 451, 452, 554, 550):
            if exc.smtp_code in (450, 451, 452):
                return EmailProviderError(
                    ProviderErrorCode.MESSAGE_REJECTED,
                    "The SMTP server could not accept the message right now",
                )
            return EmailProviderError(
                ProviderErrorCode.MESSAGE_REJECTED,
                "The SMTP server rejected the message",
            )
        if isinstance(exc, (smtplib.SMTPConnectError, smtplib.SMTPServerDisconnected, smtplib.SMTPHeloError)):
            return EmailProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "The SMTP server could not be reached",
            )
        if isinstance(exc, (smtplib.SMTPNotSupportedError, smtplib.SMTPException)):
            return EmailProviderError(
                ProviderErrorCode.MESSAGE_REJECTED,
                "The SMTP server rejected the SMTP dialogue",
            )
        if isinstance(exc, (TimeoutError, socket.timeout, OSError, ssl.SSLError)):
            return EmailProviderError(
                ProviderErrorCode.PROVIDER_UNAVAILABLE,
                "The SMTP server is unreachable",
            )
        return EmailProviderError(
            ProviderErrorCode.UNKNOWN_PROVIDER_ERROR,
            "SMTP delivery failed",
        )

    # ------------------------------------------------------------------ #
    # Connection lifecycle (real handshake)
    # ------------------------------------------------------------------ #
    def validate_connection(self, config: ProviderConnectionConfig) -> ValidationResult:
        shape_ok, shape_message = self._validate_shape(config)
        if not shape_ok:
            return ValidationResult(valid=False, message=shape_message)
        if not config.credential_reference:
            return ValidationResult(
                valid=False,
                message="SMTP credentials have not been stored for this connection",
                error_code=ProviderErrorCode.AUTH_REQUIRED,
            )
        host, port, security, username, password = self._connection_settings(config)
        try:
            self._validate_destination(config)
            client = self._connect(host, port, security)
        except SMTPPolicyViolation:
            return ValidationResult(
                valid=False,
                message="SMTP destination violates the sender security policy",
                error_code=ProviderErrorCode.PERMISSION_DENIED,
            )
        except EmailProviderError:
            return ValidationResult(
                valid=False,
                message="SMTP connection could not be established",
                error_code=ProviderErrorCode.PROVIDER_UNAVAILABLE,
            )
        except Exception:
            return ValidationResult(
                valid=False,
                message="SMTP connection could not be established",
                error_code=ProviderErrorCode.PROVIDER_UNAVAILABLE,
            )
        try:
            if username is not None:
                client.login(str(username), str(password or ""))
        except smtplib.SMTPAuthenticationError:
            return ValidationResult(
                valid=False,
                message="SMTP authentication failed for this mailbox",
                error_code=ProviderErrorCode.AUTH_FAILED,
            )
        finally:
            try:
                client.quit()
            except smtplib.SMTPServerDisconnected:
                pass
        return ValidationResult(valid=True, message="SMTP connection verified")

    def connect(self, config: ProviderConnectionConfig) -> None:
        result = self.validate_connection(config)
        if not result.valid:
            raise EmailProviderError(result.error_code or ProviderErrorCode.AUTH_FAILED, result.message)

    def disconnect(self, config: ProviderConnectionConfig) -> None:
        return None

    def refresh_credentials(self, config: ProviderConnectionConfig) -> CredentialRotationResult:
        raise EmailProviderError(
            ProviderErrorCode.AUTH_REQUIRED,
            "SMTP credentials are static; upload new credentials to rotate",
        )

    # ------------------------------------------------------------------ #
    # Sending (smtplib, RFC 5322 message, policy-checked destination)
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
        try:
            self._validate_destination(config)
        except SMTPPolicyViolation as exc:
            raise EmailProviderError(ProviderErrorCode.PERMISSION_DENIED, str(exc)) from None
        host, port, security, username, password = self._connection_settings(config)
        client = None
        try:
            client = self._connect(host, port, security)
            if username is not None:
                client.login(str(username), str(password or ""))
            sent = client.send_message(_compose_message(message))
        except EmailProviderError:
            raise
        except Exception as exc:
            raise self._map_exception(exc) from exc
        finally:
            if client is not None:
                try:
                    client.quit()
                except Exception:
                    pass
        # smtplib return is a dict of refused recipients — empty means full accept.
        if sent:
            raise EmailProviderError(
                ProviderErrorCode.INVALID_RECIPIENT,
                "The SMTP server refused one or more recipients",
            )
        # SMTP gives no provider message id; return the stable accepted handle.
        return f"accepted:{_stable_id(message)}"

    # ------------------------------------------------------------------ #
    # Discovery / inbox (not supported by generic SMTP; IMAP comes later)
    # ------------------------------------------------------------------ #
    def discover_senders(self, config: ProviderConnectionConfig) -> DiscoveryResult:
        return DiscoveryResult(senders=(), errors=("Generic SMTP does not support sender discovery",))

    def get_sender_profile(self, config: ProviderConnectionConfig, email: str) -> ProviderSenderProfile | None:
        if config.email and config.email.lower() == email.lower():
            hint = str(config.metadata.get("display_name") or "").strip()
            return ProviderSenderProfile(email=config.email, display_name=hint or None)
        return None


def _validate_recipients(*recipients: str) -> None:
    for recipient in recipients:
        if not recipient or not _EMAIL_RE.match(recipient):
            raise EmailProviderError(
                ProviderErrorCode.INVALID_RECIPIENT,
                f"Invalid recipient address: {recipient}",
            )


def _compose_message(message: EmailMessage) -> RFCMessage:
    """Build an RFC 5322 message with text/plain + text/html alternatives."""
    msg = RFCMessage()
    msg["From"] = message.from_email
    msg["To"] = ", ".join(message.to)
    if message.cc:
        msg["Cc"] = ", ".join(message.cc)
    if message.bcc:
        # Bcc must never be written into the on-wire headers.
        msg["Bcc"] = ", ".join(message.bcc)
    msg["Subject"] = message.subject
    msg["Date"] = formatdate(localtime=True)
    msg["Message-ID"] = make_msgid()
    if message.reply_to:
        msg["Reply-To"] = message.reply_to
    for name, value in message.headers.items():
        msg[name] = value
    if message.html_body and message.text_body is not None:
        msg.set_content(message.text_body)
        msg.add_alternative(message.html_body, subtype="html")
    elif message.html_body:
        msg.set_content(message.html_body, subtype="html")
    else:
        msg.set_content(message.text_body or "")
    for attachment in message.attachments:
        msg.add_attachment(
            attachment.content,
            maintype=attachment.mime_type.split("/", 1)[0] or "application",
            subtype=attachment.mime_type.split("/", 1)[1] if "/" in attachment.mime_type else "octet-stream",
            filename=attachment.filename,
        )
    return msg


def _stable_id(message: EmailMessage) -> str:
    import hashlib

    to = ",".join(sorted(message.to))
    digest = hashlib.sha256(f"{message.from_email}|{to}|{message.subject}".encode()).hexdigest()[:16]
    return digest