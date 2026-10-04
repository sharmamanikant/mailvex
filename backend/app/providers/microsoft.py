from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

import requests

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

GRAPH_BASE = "https://graph.microsoft.com/v1.0"


class MicrosoftGraphError(RuntimeError):
    def __init__(self, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.retry_after = retry_after


class MicrosoftGraphProvider(EmailProviderInterface):
    """Microsoft Graph adapter; access tokens are resolved server-side."""

    supports_inbox: bool = True

    def __init__(self, profile: ProviderProfile, access_token: str | None = None, refresh_token: str | None = None, client_id: str | None = None, client_secret: str | None = None, tenant_id: str = "common") -> None:
        self.profile = profile
        self.access_token = access_token
        self.connected = False
        self.refresh_token = refresh_token
        self.client_id = client_id
        self.client_secret = client_secret
        self.tenant_id = tenant_id
        self.expires_at: datetime | None = None
        self.session = requests.Session()

    def connect(self) -> None:
        if not self.access_token and not (self.refresh_token and self.client_id and self.client_secret):
            raise MicrosoftGraphError("Microsoft credentials are not configured")
        if self.refresh_token and self.client_id and self.client_secret:
            self.refresh_credentials()
        if not self.access_token:
            raise MicrosoftGraphError("Microsoft credentials are not configured")
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False
        self.access_token = None
        self.session.close()

    def authenticate(self) -> bool:
        return self.connected and bool(self.access_token)

    def _request(self, method: str, path: str, **kwargs: Any) -> dict[str, Any]:
        if not self.authenticate():
            raise MicrosoftGraphError("Microsoft provider is not connected")
        headers = kwargs.pop("headers", {})
        headers["Authorization"] = f"Bearer {self.access_token}"
        headers.setdefault("Content-Type", "application/json")
        response = self.session.request(method, f"{GRAPH_BASE}{path}", headers=headers, timeout=15, **kwargs)
        if response.status_code in (429, 503):
            retry_after = int(response.headers.get("Retry-After", "60"))
            raise MicrosoftGraphError("Microsoft Graph throttled the request; defer retry according to scheduler policy", retry_after=retry_after)
        if not response.ok:
            raise MicrosoftGraphError("Microsoft Graph rejected the request")
        return response.json() if response.content else {}

    def get_profile(self) -> ProviderProfile:
        data = self._request("GET", "/me?$select=mail,userPrincipalName,displayName,mailboxSettings")
        email = data.get("mail") or data.get("userPrincipalName")
        if not email:
            raise MicrosoftGraphError("Microsoft mailbox profile has no address")
        timezone_name = data.get("mailboxSettings", {}).get("timeZone", self.profile.timezone)
        return ProviderProfile(email=email, display_name=data.get("displayName") or self.profile.display_name, reply_to=self.profile.reply_to, timezone=timezone_name)

    def send(self, message: ProviderMessage) -> ProviderResult:
        payload: dict[str, Any] = {"message": {"subject": message.subject, "body": {"contentType": "HTML", "content": message.html_body}, "toRecipients": [{"emailAddress": {"address": message.recipient}}]}, "saveToSentItems": True}
        if message.reply_to:
            payload["message"]["replyTo"] = [{"emailAddress": {"address": message.reply_to}}]
        self._request("POST", "/me/sendMail", json=payload)
        now = datetime.now(UTC)
        return ProviderResult(f"graph-{now.timestamp()}", now, "MICROSOFT")

    def get_message(self, provider_message_id: str) -> dict[str, Any] | None:
        return self._request("GET", f"/me/messages/{provider_message_id}")

    def get_thread(self, provider_thread_id: str) -> dict[str, Any] | None:
        return self._request("GET", f"/me/messages?$filter=conversationId eq '{provider_thread_id}'")

    # -------------------------------------------------------------- inbox

    def _parse_message(self, data: dict[str, Any]) -> ProviderInboxMessage:
        from_address = (data.get("from", {}).get("emailAddress", {}).get("address") or "")
        to_addresses = [
            item.get("emailAddress", {}).get("address")
            for item in (data.get("toRecipients", []) or [])
            if item.get("emailAddress", {}).get("address")
        ]
        to_email = ", ".join(to_addresses)
        direction = "OUTBOUND" if from_address.lower() == self.profile.email.lower() else "INBOUND"
        raw_time = data.get("receivedDateTime")
        received = (
            datetime.fromisoformat(raw_time.replace("Z", "+00:00"))
            if raw_time
            else datetime.now(UTC)
        )
        return ProviderInboxMessage(
            id=str(data.get("id", "")),
            thread_id=str(data.get("conversationId", "")),
            direction=direction,
            from_email=from_address,
            to_email=to_email,
            subject=str(data.get("subject", "")),
            body_text=str(data.get("bodyPreview", "")),
            body_html=None,
            received_at=received,
            in_reply_to=str(data.get("internetMessageId") or ""),
            references=None,
            provider="MICROSOFT",
        )

    def _message_select(self) -> str:
        return (
            "id,conversationId,from,toRecipients,subject,receivedDateTime,bodyPreview"
        )

    def list_threads(self, limit: int = 50) -> list[ProviderThread]:
        data = self._request(
            "GET",
            f"/me/messages?$top={limit}&$orderby=receivedDateTime%20desc&$select={self._message_select()}",
        )
        threads: list[ProviderThread] = []
        seen: set[str] = set()
        for item in data.get("value", []) or []:
            conv_id = str(item.get("conversationId", ""))
            if not conv_id or conv_id in seen:
                continue
            seen.add(conv_id)
            parsed = self._parse_message(item)
            threads.append(
                ProviderThread(
                    id=conv_id,
                    subject=parsed.subject,
                    last_message_at=parsed.received_at or datetime.now(UTC),
                    from_email=parsed.from_email,
                    snippet=parsed.body_text,
                )
            )
        return threads

    def get_messages(self, provider_thread_id: str) -> list[ProviderInboxMessage]:
        data = self._request(
            "GET",
            f"/me/messages?$filter=conversationId eq '{provider_thread_id}'&$select={self._message_select()}",
        )
        return [self._parse_message(item) for item in data.get("value", []) or []]

    def sync_messages(self, limit: int = 50) -> list[ProviderInboxMessage]:
        data = self._request(
            "GET",
            f"/me/messages?$top={limit}&$orderby=receivedDateTime%20desc&$select={self._message_select()}",
        )
        return [self._parse_message(item) for item in data.get("value", []) or []]

    def get_events(self, cursor: str | None = None) -> list[dict[str, Any]]:
        return []

    def create_draft(self, message: ProviderMessage) -> str:
        data = self._request("POST", "/me/messages", json={"subject": message.subject, "body": {"contentType": "HTML", "content": message.html_body}, "toRecipients": [{"emailAddress": {"address": message.recipient}}]})
        return str(data["id"])

    def health_check(self) -> bool:
        try:
            self.get_profile()
            return True
        except MicrosoftGraphError:
            return False

    def _exchange(self, data: dict[str, str]) -> dict[str, Any]:
        response = self.session.post(f"https://login.microsoftonline.com/{self.tenant_id}/oauth2/v2.0/token", data=data, timeout=15)
        if not response.ok:
            raise SenderUnavailableError("Microsoft credentials are not recoverable; reconnect required")
        return response.json()

    def refresh_credentials(self) -> ProviderCredentials:
        if not (self.refresh_token and self.client_id and self.client_secret):
            raise SenderUnavailableError("Microsoft credentials are not recoverable; reconnect required")
        try:
            token_data = self._exchange(
                {
                    "client_id": self.client_id,
                    "client_secret": self.client_secret,
                    "refresh_token": self.refresh_token,
                    "grant_type": "refresh_token",
                    "scope": " ".join(self.requested_scopes()),
                }
            )
        except (requests.RequestException, ValueError) as exc:
            raise SenderUnavailableError("Microsoft credentials are not recoverable; reconnect required") from exc
        if not token_data.get("access_token"):
            raise SenderUnavailableError("Microsoft credentials are not recoverable; reconnect required")
        self.access_token = str(token_data["access_token"])
        if token_data.get("refresh_token"):
            self.refresh_token = str(token_data["refresh_token"])
        expires_in = int(token_data.get("expires_in", 3600))
        self.expires_at = datetime.now(UTC) + timedelta(seconds=expires_in)
        return ProviderCredentials(
            access_token=self.access_token,
            refresh_token=self.refresh_token,
            expires_at=self.expires_at,
            scopes=self.requested_scopes(),
        )

    def requested_scopes(self) -> list[str]:
        return ["openid", "profile", "email", "offline_access", "User.Read", "Mail.Read", "Mail.Send"]
