from __future__ import annotations

import base64
from datetime import UTC, datetime
from email.message import EmailMessage
from typing import Any

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from .base import (
    EmailProviderInterface,
    ProviderCredentials,
    ProviderInboxMessage,
    ProviderMessage,
    ProviderProfile,
    ProviderResult,
    ProviderThread,
    SenderUnavailableError,
)

GMAIL_SCOPES = ("https://www.googleapis.com/auth/gmail.send", "https://www.googleapis.com/auth/gmail.readonly")


class GmailProvider(EmailProviderInterface):
    """Official Gmail API adapter; credentials are supplied by the server only."""

    supports_inbox: bool = True

    def __init__(self, profile: ProviderProfile, credentials: Credentials | None = None) -> None:
        self.profile = profile
        self.credentials = credentials
        self._service: Any = None

    def connect(self) -> None:
        if self.credentials is None:
            raise RuntimeError("Gmail credentials are not configured")
        if self.credentials.expired and self.credentials.refresh_token:
            self.credentials.refresh(Request())
        self._service = build("gmail", "v1", credentials=self.credentials, cache_discovery=False)

    def disconnect(self) -> None:
        self._service = None
        self.credentials = None

    def authenticate(self) -> bool:
        return self._service is not None and self.credentials is not None and bool(self.credentials.valid)

    def get_profile(self) -> ProviderProfile:
        if self._service is None:
            self.connect()
        profile = self._service.users().getProfile(userId="me").execute()
        return ProviderProfile(email=profile["emailAddress"], display_name=self.profile.display_name, reply_to=self.profile.reply_to, timezone=self.profile.timezone)

    def send(self, message: ProviderMessage) -> ProviderResult:
        if self._service is None:
            self.connect()
        email = EmailMessage()
        email["To"] = message.recipient
        email["Subject"] = message.subject
        email["From"] = self.profile.email
        if message.reply_to:
            email["Reply-To"] = message.reply_to
        for name, value in message.headers.items():
            email[name] = value
        email.set_content(message.text_body or "")
        email.add_alternative(message.html_body, subtype="html")
        try:
            result = self._service.users().messages().send(userId="me", body={"raw": base64.urlsafe_b64encode(email.as_bytes()).decode()}).execute()
        except HttpError as exc:
            if exc.resp.status in (403, 429):
                raise RuntimeError("Gmail throttled the request; defer retry according to scheduler policy") from exc
            raise RuntimeError("Gmail rejected the request") from exc
        return ProviderResult(result["id"], datetime.now(UTC), "GOOGLE")

    def get_message(self, provider_message_id: str) -> dict[str, Any] | None:
        if self._service is None:
            self.connect()
        return self._service.users().messages().get(userId="me", id=provider_message_id, format="metadata").execute()

    def get_thread(self, provider_thread_id: str) -> dict[str, Any] | None:
        if self._service is None:
            self.connect()
        return self._service.users().threads().get(userId="me", id=provider_thread_id, format="metadata").execute()

    # -------------------------------------------------------------- inbox

    def _fetch_message_full(self, message_id: str) -> dict[str, Any] | None:
        if self._service is None:
            self.connect()
        data = self._service.users().messages().get(
            userId="me", id=message_id, format="full"
        ).execute()
        return data

    @staticmethod
    def _header(payload: dict[str, Any], name: str) -> str:
        for header in payload.get("headers", []):
            if header.get("name", "").lower() == name.lower():
                return str(header.get("value", ""))
        return ""

    def _parse_message(self, data: dict[str, Any]) -> ProviderInboxMessage:
        payload = data.get("payload", {})
        internal = int(float(data.get("internalDate", "0") or 0))
        received = datetime.fromtimestamp(internal / 1000, tz=UTC) if internal else datetime.now(UTC)
        body = self._extract_text(payload)
        return ProviderInboxMessage(
            id=str(data.get("id", "")),
            thread_id=str(data.get("threadId", "")),
            direction="INBOUND" if self._header(payload, "From") else "OUTBOUND",
            from_email=self._header(payload, "From"),
            to_email=self._header(payload, "To"),
            subject=self._header(payload, "Subject"),
            body_text=body,
            received_at=received,
            in_reply_to=self._header(payload, "In-Reply-To") or None,
            references=self._header(payload, "References") or None,
            provider="GOOGLE",
        )

    def _extract_text(self, payload: dict[str, Any]) -> str:
        if payload.get("body", {}).get("data"):
            return self._decode(payload["body"]["data"])
        for part in payload.get("parts", []) or []:
            if part.get("mimeType") == "text/plain" and part.get("body", {}).get("data"):
                return self._decode(part["body"]["data"])
            nested = self._extract_text(part)
            if nested:
                return nested
        return ""

    @staticmethod
    def _decode(encoded: str) -> str:
        import urllib.parse

        text = base64.urlsafe_b64decode(encoded).decode("utf-8", errors="replace")
        return urllib.parse.unquote(text)

    def list_threads(self, limit: int = 50) -> list[ProviderThread]:
        if self._service is None:
            self.connect()
        result = self._service.users().threads().list(
            userId="me", q="in:inbox", maxResults=limit
        ).execute()
        threads: list[ProviderThread] = []
        for entry in result.get("threads", []) or []:
            raw = self._service.users().threads().get(
                userId="me", id=entry["id"], format="full"
            ).execute()
            messages = raw.get("messages", []) or []
            if not messages:
                continue
            last = self._parse_message(messages[-1])
            threads.append(
                ProviderThread(
                    id=entry["id"],
                    subject=last.subject,
                    last_message_at=last.received_at or datetime.now(UTC),
                    from_email=last.from_email,
                    snippet=last.body_text,
                )
            )
        return threads

    def get_messages(self, provider_thread_id: str) -> list[ProviderInboxMessage]:
        if self._service is None:
            self.connect()
        result = self._service.users().threads().get(
            userId="me", id=provider_thread_id, format="full"
        ).execute()
        return [self._parse_message(item) for item in result.get("messages", []) or []]

    def sync_messages(self, limit: int = 50) -> list[ProviderInboxMessage]:
        if self._service is None:
            self.connect()
        result = self._service.users().messages().list(
            userId="me", q="in:inbox", maxResults=limit
        ).execute()
        items: list[ProviderInboxMessage] = []
        for entry in result.get("messages", []) or []:
            data = self._fetch_message_full(entry["id"])
            if data is not None:
                items.append(self._parse_message(data))
        return items

    def get_events(self, cursor: str | None = None) -> list[dict[str, Any]]:
        return []

    def create_draft(self, message: ProviderMessage) -> str:
        if self._service is None:
            self.connect()
        email = EmailMessage()
        email["To"] = message.recipient
        email["Subject"] = message.subject
        email.set_content(message.text_body or "")
        email.add_alternative(message.html_body, subtype="html")
        result = self._service.users().drafts().create(userId="me", body={"message": {"raw": base64.urlsafe_b64encode(email.as_bytes()).decode()}}).execute()
        return result["id"]

    def health_check(self) -> bool:
        try:
            self.get_profile()
            return True
        except (HttpError, RuntimeError):
            return False

    def refresh_credentials(self) -> ProviderCredentials:
        if self.credentials is None:
            raise SenderUnavailableError("Closing the loop requires a reconnect")
        try:
            self.credentials.refresh(Request())
        except Exception as exc:
            # Chrome/refresh error -> instruct the user to re-auth, do not
            # surface any provider exception detail.
            raise SenderUnavailableError("Your Google account needs to be reconnected.") from exc
        assert self.credentials.token is not None
        return ProviderCredentials(
            access_token=self.credentials.token,
            refresh_token=self.credentials.refresh_token,
            expires_at=self.credentials.expiry or datetime.now(UTC),
            scopes=list(self.credentials.scopes or GMAIL_SCOPES),
        )

    def requested_scopes(self) -> list[str]:
        return list(GMAIL_SCOPES)
