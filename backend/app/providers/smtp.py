from __future__ import annotations

import smtplib
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

from app.security.smtp_policy import Resolver

from .base import (
    EmailProviderInterface,
    ProviderCredentials,
    ProviderMessage,
    ProviderProfile,
    ProviderResult,
    SenderUnavailableError,
)


class SMTPProviderError(RuntimeError):
    pass


class SMTPPolicyError(SMTPProviderError):
    """Raised when an SMTP destination violates the sender security policy."""


class SMTPProvider(EmailProviderInterface):
    """SMTP adapter with explicit TLS modes, server-side credentials and SSRF
    hardening. The password is only ever used locally for SMTP AUTH and is never
    logged, returned or accepted back after construction.
    """

    def __init__(
        self,
        profile: ProviderProfile,
        host: str | None = None,
        port: int | None = None,
        tls_mode: str | None = None,
        username: str | None = None,
        password: str | None = None,
        *,
        allow_private_hosts: bool = False,
        allow_plaintext_auth: bool = False,
        allow_insecure_ports: bool = False,
        validate: bool = True,
        resolver: Resolver | None = None,
    ) -> None:
        self.profile = profile
        self.host = host
        self.port = port
        self.tls_mode = tls_mode
        self.username = username
        self._password = password
        self._allow_private_hosts = allow_private_hosts
        self._allow_plaintext_auth = allow_plaintext_auth
        self._allow_insecure_ports = allow_insecure_ports
        self._validate = validate
        self._resolver = resolver
        self.connection: smtplib.SMTP | smtplib.SMTP_SSL | None = None
        self._tls_negotiated = False

    def _check(self) -> None:
        if not self.host or not self.port or self.tls_mode not in {"TLS", "STARTTLS", "SSL"}:
            raise SMTPProviderError("SMTP host, port, and TLS mode are required")

    def _policy_check(self) -> None:
        from app.security.smtp_policy import SMTPPolicyViolation, validate_destination

        try:
            validate_destination(
                str(self.host),
                port=self.port,
                tls_mode=self.tls_mode,
                username=self.username,
                password=self._password,
                allow_private_hosts=self._allow_private_hosts,
                allow_plaintext_auth=self._allow_plaintext_auth,
                allow_insecure_ports=self._allow_insecure_ports,
                resolver=self._resolver,
            )
        except SMTPPolicyViolation as exc:
            raise SMTPPolicyError(str(exc)) from exc

    def _open(self) -> None:
        assert self.host is not None and self.port is not None
        self._tls_negotiated = False
        try:
            if self.tls_mode == "SSL" or (self.tls_mode == "TLS" and self.port == 465):
                self.connection = smtplib.SMTP_SSL(self.host, self.port, timeout=15)
                self._tls_negotiated = True
            else:
                self.connection = smtplib.SMTP(self.host, self.port, timeout=15)
                self.connection.ehlo()
                if self.tls_mode in {"STARTTLS", "TLS"}:
                    self.connection.starttls()
                    self._tls_negotiated = True
                    self.connection.ehlo()
        except (OSError, smtplib.SMTPException) as exc:
            self.disconnect()
            raise SMTPProviderError("SMTP connection or TLS negotiation failed") from exc

    def _login(self) -> None:
        if self.username and self._password:
            try:
                assert self.connection is not None
                self.connection.login(self.username, self._password)
            except (OSError, smtplib.SMTPException) as exc:
                self.disconnect()
                raise SMTPProviderError("SMTP authentication failed") from exc

    def connect(self) -> None:
        self._check()
        if self._validate:
            self._policy_check()
        self._open()
        self._login()

    def disconnect(self) -> None:
        if self.connection is not None:
            try:
                self.connection.quit()
            except smtplib.SMTPException:
                self.connection.close()
        self.connection = None

    def authenticate(self) -> bool:
        return self.connection is not None

    def verify(self) -> dict[str, bool]:
        """Run the stages of a connection test without sending any email.

        Returns a per-stage result: dns, tcp, tls, auth. Raises SMTPProviderError
        with a safe (non-credential) message if a stage fails.
        """
        self._check()
        if self._validate:
            self._policy_check()
        result = {"dns": True, "tcp": False, "tls": False, "auth": False}
        self._open()
        result["tcp"] = True
        result["tls"] = self._tls_negotiated
        if self.username:
            try:
                self._login()
                result["auth"] = True
            except SMTPProviderError:
                result["auth"] = False
        return result

    def get_profile(self) -> ProviderProfile:
        return self.profile

    def send(self, message: ProviderMessage) -> ProviderResult:
        if not self.authenticate():
            self.connect()
        email = EmailMessage()
        email["From"] = self.profile.email
        email["To"] = message.recipient
        email["Subject"] = message.subject
        if message.reply_to:
            email["Reply-To"] = message.reply_to
        email.set_content(message.text_body or "")
        email.add_alternative(message.html_body, subtype="html")
        try:
            assert self.connection is not None
            self.connection.send_message(email)
        except (OSError, smtplib.SMTPException) as exc:
            raise SMTPProviderError("SMTP message was not accepted") from exc
        now = datetime.now(UTC)
        return ProviderResult(f"smtp-{now.timestamp()}", now, "SMTP")

    def get_message(self, provider_message_id: str) -> dict[str, Any] | None:
        return None

    def get_thread(self, provider_thread_id: str) -> dict[str, Any] | None:
        return None

    def get_events(self, cursor: str | None = None) -> list[dict[str, Any]]:
        return []

    def create_draft(self, message: ProviderMessage) -> str:
        raise SMTPProviderError("SMTP does not support server-side drafts")

    def health_check(self) -> bool:
        try:
            if not self.authenticate():
                self.connect()
            return self.authenticate()
        except SMTPProviderError:
            return False
        finally:
            self.disconnect()

    def refresh_credentials(self) -> ProviderCredentials:
        raise SenderUnavailableError("SMTP accounts use server-side passwords and do not support token refresh")

    def requested_scopes(self) -> list[str]:
        return []
