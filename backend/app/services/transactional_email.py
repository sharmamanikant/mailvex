from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage
from typing import Protocol

from app.core.config import settings

logger = logging.getLogger(__name__)


class TransactionalEmailProvider(Protocol):
    def send(self, recipient: str, subject: str, text_body: str, html_body: str) -> None: ...


def send_reauth_notification(recipient: str, display_name: str) -> None:
    """Best-effort notification when a sender needs re-authentication."""
    if not settings.transactional_smtp_host or not settings.transactional_smtp_username:
        logger.warning("Transactional SMTP not configured; skipping reauth notification for %s", recipient)
        return
    provider = SMTPTransactionalEmailProvider(
        host=settings.transactional_smtp_host,
        port=settings.transactional_smtp_port,
        username=settings.transactional_smtp_username,
        password=settings.transactional_smtp_password,
        sender=settings.transactional_email_from or settings.transactional_smtp_username,
    )
    subject = "Action required: Reconnect your sending account"
    text_body = (
        f"Hi {display_name},\n\n"
        "One of your sending accounts needs to be reconnected because its "
        "authorization expired or was revoked. Until it is reconnected, "
        "messages from this sender will be paused.\n\n"
        "Please sign in to CR+CRM and reconnect the account from the Senders page."
    )
    html_body = (
        f"<p>Hi {display_name},</p>"
        "<p>One of your sending accounts needs to be <strong>reconnected</strong> "
        "because its authorization expired or was revoked. Until it is reconnected, "
        "messages from this sender will be paused.</p>"
        "<p>Please sign in to CR+CRM and reconnect the account from the Senders page.</p>"
    )
    provider.send(recipient, subject, text_body, html_body)


class SMTPTransactionalEmailProvider:
    def __init__(self, host: str, port: int, username: str, password: str, sender: str, tls_mode: str = "STARTTLS") -> None:
        self.host = host
        self.port = port
        self.username = username
        self.password = password
        self.sender = sender
        self.tls_mode = tls_mode

    def send(self, recipient: str, subject: str, text_body: str, html_body: str) -> None:
        if self.tls_mode not in {"SSL", "STARTTLS", "TLS"}:
            raise ValueError("Unsupported transactional SMTP TLS mode")
        if not self.username or not self.password:
            raise ValueError("Transactional SMTP credentials are required")
        message = EmailMessage()
        message["From"] = self.sender
        message["To"] = recipient
        message["Subject"] = subject
        message.set_content(text_body)
        message.add_alternative(html_body, subtype="html")
        connection: smtplib.SMTP | smtplib.SMTP_SSL
        if self.tls_mode == "SSL":
            connection = smtplib.SMTP_SSL(self.host, self.port, timeout=15)
        else:
            connection = smtplib.SMTP(self.host, self.port, timeout=15)
            connection.ehlo()
            if self.tls_mode in {"STARTTLS", "TLS"}:
                connection.starttls()
                connection.ehlo()
        try:
            connection.login(self.username, self.password)
            connection.send_message(message)
        finally:
            connection.quit()
