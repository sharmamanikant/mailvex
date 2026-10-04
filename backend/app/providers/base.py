from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


@dataclass(frozen=True)
class ProviderProfile:
    email: str
    display_name: str | None = None
    reply_to: str | None = None
    timezone: str = "UTC"


@dataclass(frozen=True)
class ProviderMessage:
    recipient: str
    subject: str
    html_body: str
    text_body: str | None = None
    reply_to: str | None = None
    headers: dict[str, str] = field(default_factory=dict)


@dataclass(frozen=True)
class ProviderResult:
    provider_message_id: str
    accepted_at: datetime
    provider: str


@dataclass(frozen=True)
class ProviderThread:
    """A normalized mailbox thread returned from a provider's inbox."""

    id: str
    subject: str
    last_message_at: datetime
    from_email: str | None = None
    from_name: str | None = None
    snippet: str | None = None


@dataclass(frozen=True)
class ProviderInboxMessage:
    """A normalized message returned from a provider's mailbox."""

    id: str
    thread_id: str
    direction: str  # INBOUND | OUTBOUND
    from_email: str
    to_email: str
    subject: str
    body_text: str
    body_html: str | None = None
    received_at: datetime | None = None
    in_reply_to: str | None = None
    references: str | None = None
    provider: str = ""


@dataclass(frozen=True)
class ProviderCredentials:
    """Refreshed OAuth token state returned for server-side persistence.

    Carries the tokens produced by a successful refresh so the service layer
    can encrypt and store them. Implementations never surface these to clients.
    """

    access_token: str
    refresh_token: str | None
    expires_at: datetime
    scopes: list[str]


class SenderUnavailableError(RuntimeError):
    """Raised when a provider cannot continue (auth/credential failure)."""


class EmailProviderInterface(ABC):
    """Provider boundary; implementations never expose stored credentials."""

    @abstractmethod
    def connect(self) -> None: ...

    @abstractmethod
    def disconnect(self) -> None: ...

    @abstractmethod
    def authenticate(self) -> bool: ...

    @abstractmethod
    def get_profile(self) -> ProviderProfile: ...

    @abstractmethod
    def send(self, message: ProviderMessage) -> ProviderResult: ...

    @abstractmethod
    def get_message(self, provider_message_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def get_thread(self, provider_thread_id: str) -> dict[str, Any] | None: ...

    @abstractmethod
    def get_events(self, cursor: str | None = None) -> list[dict[str, Any]]: ...

    @abstractmethod
    def create_draft(self, message: ProviderMessage) -> str: ...

    @abstractmethod
    def health_check(self) -> bool: ...

    @abstractmethod
    def refresh_credentials(self) -> ProviderCredentials: ...

    @abstractmethod
    def requested_scopes(self) -> list[str]: ...

    # ------------------------------------------------------------------ #
    # Inbox support (Phase 17). Not all providers can read a mailbox.
    # Providers that cannot (e.g. SMTP) inherit the inert default below and
    # never pretend to synchronize inboxes.
    # ------------------------------------------------------------------ #
    supports_inbox: bool = False

    def list_threads(self, limit: int = 50) -> list[ProviderThread]:
        return []

    def get_messages(self, provider_thread_id: str) -> list[ProviderInboxMessage]:
        return []

    def sync_messages(self, limit: int = 50) -> list[ProviderInboxMessage]:
        return []
