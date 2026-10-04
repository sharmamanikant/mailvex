from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import datetime
from enum import StrEnum

__all__ = [
    "Attachment",
    "CredentialRotationResult",
    "DeliveryEventCursor",
    "DiscoveryResult",
    "EmailMessage",
    "EmailProviderBase",
    "EmailProviderError",
    "ProviderCapabilities",
    "ProviderConnectionConfig",
    "ProviderConnectionRequirementError",
    "ProviderErrorCode",
    "ProviderMethodNotImplemented",
    "ProviderSenderProfile",
    "ValidationResult",
    "WebhookEvent",
]


@dataclass(frozen=True)
class ProviderCapabilities:
    """Static capability metadata for a provider integration.

    UI and API consumers use these flags to decide which flows to offer
    (OAuth wizard vs. API-key form, sender discovery, webhooks, inbox sync).
    """

    provider_name: str
    display_name: str
    connection_types: tuple[str, ...] = ()
    supports_oauth: bool = False
    supports_api_key: bool = False
    supports_smtp: bool = False
    supports_sender_discovery: bool = False
    supports_webhooks: bool = False
    supports_inbox_sync: bool = False


@dataclass(frozen=True)
class ProviderConnectionConfig:
    """Everything a provider needs at runtime — never raw secrets.

    The credential reference is an opaque encrypted pointer produced by the
    secure credential store. Implementations must treat it as opaque.
    """

    connection_type: str  # OAUTH | API_KEY | SMTP
    external_account_id: str | None = None
    email: str | None = None
    metadata: dict[str, object] = field(default_factory=dict)
    credential_reference: str | None = None
    credential_version: str = "v1"
    credential_expires_at: datetime | None = None


@dataclass(frozen=True)
class ProviderSenderProfile:
    """A sender mailbox discovered on (or registered against) a connection."""

    email: str
    display_name: str | None = None
    external_sender_id: str | None = None
    verified: bool = False


@dataclass(frozen=True)
class Attachment:
    """A single email attachment payload (bytes never touch audit/API output)."""

    filename: str
    content: bytes
    mime_type: str


@dataclass(frozen=True)
class EmailMessage:
    """Normalized email model used by provider ``send_message``.

    Field names mirror RFC 5322 headers; ``from_email`` is the sender mailbox
    (the ``from`` keyword is reserved in Python). Attachments are carried as
    raw bytes and must never be persisted in audit/API responses.
    """

    from_email: str
    to: tuple[str, ...]
    subject: str
    text_body: str | None = None
    html_body: str | None = None
    cc: tuple[str, ...] = ()
    bcc: tuple[str, ...] = ()
    reply_to: str | None = None
    headers: dict[str, str] = field(default_factory=dict)
    attachments: tuple[Attachment, ...] = ()


class ProviderErrorCode(StrEnum):
    """Normalized, provider-agnostic error taxonomy (Phase 10B spec item 17).

    Providers map their native failures onto these codes so callers, UI and
    audit records stay consistent without exposing provider internals.
    """

    AUTH_REQUIRED = "AUTH_REQUIRED"
    AUTH_FAILED = "AUTH_FAILED"
    TOKEN_EXPIRED = "TOKEN_EXPIRED"
    PERMISSION_DENIED = "PERMISSION_DENIED"
    RATE_LIMITED = "RATE_LIMITED"
    QUOTA_EXCEEDED = "QUOTA_EXCEEDED"
    INVALID_RECIPIENT = "INVALID_RECIPIENT"
    MESSAGE_REJECTED = "MESSAGE_REJECTED"
    PROVIDER_UNAVAILABLE = "PROVIDER_UNAVAILABLE"
    UNKNOWN_PROVIDER_ERROR = "UNKNOWN_PROVIDER_ERROR"


@dataclass(frozen=True)
class ValidationResult:
    valid: bool
    message: str
    error_code: ProviderErrorCode | None = None


class EmailProviderError(RuntimeError):
    """Normalized provider failure carrying a :class:`ProviderErrorCode`.

    ``retry_after`` preserves the provider's back-pressure signal (HTTP
    ``Retry-After``) for rate-limit aware callers — it is never used to
    randomize delivery timing.
    """

    def __init__(self, code: ProviderErrorCode, message: str, *, retry_after: int | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.retry_after = retry_after


@dataclass(frozen=True)
class DiscoveryResult:
    senders: tuple[ProviderSenderProfile, ...] = ()
    errors: tuple[str, ...] = ()


@dataclass(frozen=True)
class CredentialRotationResult:
    """Outcome of a credential rotation; carries refs, never secrets."""

    credential_reference: str
    credential_version: str
    expires_at: datetime | None = None


@dataclass(frozen=True)
class DeliveryEventCursor:
    """Pagination cursor for delivery-event sync (later phase)."""

    cursor: str | None = None
    limit: int = 50


@dataclass(frozen=True)
class WebhookEvent:
    """A normalized provider webhook event (later phase)."""

    event_type: str
    payload: dict[str, object]


class ProviderConnectionRequirementError(ValueError):
    """Raised when a connection config violates a provider's local requirements."""


class ProviderMethodNotImplemented(NotImplementedError):
    """Raised for methods that belong to a later implementation phase.

    Phase 10A is architecture-only: no real provider API calls, no sends.
    Providers inherit these inert defaults so callers can never accidentally
    send mail or hit a provider network endpoint before the real
    implementation lands.
    """


class EmailProviderBase(ABC):
    """Provider boundary (System B foundation).

    Implementations are local-only stubs in this phase: connection/credential
    shape validation happens in-process, and anything requiring real provider
    network access (sending, discovery writes, inbox, webhooks) raises
    :class:`ProviderMethodNotImplemented`.
    """

    display_name = ""

    @abstractmethod
    def get_provider_name(self) -> str: ...

    @abstractmethod
    def get_capabilities(self) -> ProviderCapabilities: ...

    # ------------------------------------------------------------------ #
    # Connection lifecycle (architecture-only; local, no network).
    # ------------------------------------------------------------------ #
    def connect(self, config: ProviderConnectionConfig) -> None:
        """Ensure the connection config is structurally sound for this provider.

        Raises :class:`ValueError` subclasses on invalid shape; never performs
        a provider round-trip in this phase.
        """
        result = self.validate_connection(config)
        if not result.valid:
            raise ProviderConnectionRequirementError(result.message)

        return None

    def validate_connection(self, config: ProviderConnectionConfig) -> ValidationResult:
        valid, message = self._validate_shape(config)
        if valid and not config.credential_reference:
            return ValidationResult(
                valid=False,
                message="Credentials have not been stored for this connection",
            )
        return ValidationResult(valid=valid, message=message)

    def disconnect(self, config: ProviderConnectionConfig) -> None:
        return None

    def refresh_credentials(self, config: ProviderConnectionConfig) -> CredentialRotationResult:
        raise ProviderMethodNotImplemented(
            f"{self.get_provider_name()} credential refresh lands in a later phase"
        )

    # ------------------------------------------------------------------ #
    # Sender discovery (architecture-only; returns local simulation or []).
    # ------------------------------------------------------------------ #
    def discover_senders(self, config: ProviderConnectionConfig) -> DiscoveryResult:
        if not self.get_capabilities().supports_sender_discovery:
            return DiscoveryResult(
                senders=(),
                errors=(f"{self.get_provider_name()} does not support sender discovery",),
            )
        # Phase-local simulation hook: real provider discovery lands later.
        # Only the explicitly listed simulation seeds are returned, so this
        # can never fabricate real mailboxes.
        seeds = config.metadata.get("simulated_discovery")
        if isinstance(seeds, list):
            profiles = tuple(
                ProviderSenderProfile(
                    email=str(seed).strip().lower(),
                    external_sender_id=f"simulated:{seed}",
                    verified=False,
                )
                for seed in seeds
                if isinstance(seed, str) and "@" in seed
            )
            return DiscoveryResult(senders=profiles)
        return DiscoveryResult()

    def get_sender_profile(self, config: ProviderConnectionConfig, email: str) -> ProviderSenderProfile | None:
        hint = str(config.metadata.get("simulated_profile_email") or "").strip().lower()
        if hint and hint == email.lower():
            return ProviderSenderProfile(
                email=hint,
                external_sender_id=f"simulated:{hint}",
                verified=True,
            )
        return None

    # ------------------------------------------------------------------ #
    # Sending & advanced features — explicitly out of scope for Phase 10A.
    # ------------------------------------------------------------------ #
    def send_message(
        self,
        config: ProviderConnectionConfig,
        message: EmailMessage,
    ) -> str:
        raise ProviderMethodNotImplemented(
            f"{self.get_provider_name()} sending is implemented in a later phase"
        )

    def get_delivery_events(self, config: ProviderConnectionConfig, cursor: DeliveryEventCursor | None = None) -> list[dict[str, object]]:
        raise ProviderMethodNotImplemented(
            f"{self.get_provider_name()} delivery-event sync is implemented in a later phase"
        )

    def handle_webhook(self, payload: dict[str, object]) -> list[WebhookEvent]:
        raise ProviderMethodNotImplemented(
            f"{self.get_provider_name()} webhook handling is implemented in a later phase"
        )

    def sync_inbox(self, config: ProviderConnectionConfig, limit: int = 50) -> list[dict[str, object]]:
        raise ProviderMethodNotImplemented(
            f"{self.get_provider_name()} inbox sync is implemented in a later phase"
        )

    # ------------------------------------------------------------------ #
    # Per-provider local shape rules.
    # ------------------------------------------------------------------ #
    def _validate_shape(self, config: ProviderConnectionConfig) -> tuple[bool, str]:
        raise NotImplementedError