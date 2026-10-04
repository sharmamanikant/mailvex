from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

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


class MockEmailProvider(EmailProviderInterface):
    """Deterministic provider for tests and local development only."""

    supports_inbox: bool = True

    def __init__(self, provider: str, profile: ProviderProfile) -> None:
        self.provider = provider
        self.profile = profile
        self.connected = False
        self.sent: list[ProviderMessage] = []
        self.refresh_token_value: str | None = "mock-refresh-token"
        self.access_token_value: str = "mock-access-token"
        self.expires_at = datetime.now(UTC) + timedelta(hours=1)
        self.require_reauth = False
        self.last_used_at: datetime | None = None
        self.inbox: list[ProviderInboxMessage] = []
        self.fail_sync = False

    def connect(self) -> None:
        self.connected = True

    def disconnect(self) -> None:
        self.connected = False

    def authenticate(self) -> bool:
        return self.connected

    def get_profile(self) -> ProviderProfile:
        return self.profile

    def send(self, message: ProviderMessage) -> ProviderResult:
        if not self.connected:
            raise RuntimeError("Provider is not connected")
        self.last_used()
        self.sent.append(message)
        return ProviderResult(uuid4().hex, datetime.now(UTC), self.provider)

    def get_message(self, provider_message_id: str) -> dict[str, Any] | None:
        return {"id": provider_message_id} if provider_message_id else None

    def get_thread(self, provider_thread_id: str) -> dict[str, Any] | None:
        return {"id": provider_thread_id} if provider_thread_id else None

    def get_events(self, cursor: str | None = None) -> list[dict[str, Any]]:
        return []

    def create_draft(self, message: ProviderMessage) -> str:
        if not self.connected:
            raise RuntimeError("Provider is not connected")
        return uuid4().hex

    def health_check(self) -> bool:
        return self.connected

    def refresh_credentials(self) -> ProviderCredentials:
        if self.require_reauth or self.refresh_token_value is None:
            raise SenderUnavailableError("Invalid grant: mock refresh token expired")
        self.access_token_value = f"mock-access-token-{uuid4().hex[:8]}"
        self.expires_at = datetime.now(UTC) + timedelta(hours=1)
        return ProviderCredentials(
            access_token=self.access_token_value,
            refresh_token=self.refresh_token_value,
            expires_at=self.expires_at,
            scopes=["https://www.googleapis.com/auth/gmail.send"],
        )

    def requested_scopes(self) -> list[str]:
        return ["https://www.googleapis.com/auth/gmail.send"]

    def last_used(self) -> None:
        self.connected = True
        self.last_used_at = datetime.now(UTC)

    # -------------------------------------------------------------- inbox

    def list_threads(self, limit: int = 50) -> list[ProviderThread]:
        threads: dict[str, ProviderInboxMessage] = {}
        for item in sorted(
            self.inbox,
            key=lambda msg: msg.received_at or datetime.min.replace(tzinfo=UTC),
            reverse=True,
        ):
            if item.thread_id not in threads:
                threads[item.thread_id] = item
            if len(threads) >= limit:
                break
        return [
            ProviderThread(
                id=item.thread_id,
                subject=item.subject,
                last_message_at=item.received_at or datetime.now(UTC),
                from_email=item.from_email,
                snippet=item.body_text,
            )
            for item in threads.values()
        ]

    def get_messages(self, provider_thread_id: str) -> list[ProviderInboxMessage]:
        return [
            item
            for item in self.inbox
            if item.thread_id == provider_thread_id
        ]

    def sync_messages(self, limit: int = 50) -> list[ProviderInboxMessage]:
        if self.fail_sync:
            raise RuntimeError("mock sync failure")
        return list(self.inbox[:limit])
