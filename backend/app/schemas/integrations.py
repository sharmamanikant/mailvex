"""Pydantic schemas for the email-provider integration foundation (System B).

Responses never expose credential references or any stored secret — only a
``credential_configured`` flag plus non-sensitive metadata.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, EmailStr, Field, model_validator

ConnectionType = Literal["OAUTH", "API_KEY", "SMTP"]
ConnectionStatus = Literal[
    "CONNECTING",
    "CONNECTED",
    "REAUTH_REQUIRED",
    "DISCONNECTED",
    "FAILED",
    "DISABLED",
]
SenderAccountStatus = Literal["ACTIVE", "DISABLED", "INVALID", "PENDING_VERIFICATION"]


class ProviderCapabilityResponse(BaseModel):
    provider_name: str
    display_name: str
    connection_types: tuple[str, ...]
    supports_oauth: bool
    supports_api_key: bool
    supports_smtp: bool
    supports_sender_discovery: bool
    supports_webhooks: bool
    supports_inbox_sync: bool


class IntegrationCreate(BaseModel):
    provider: str = Field(min_length=1, max_length=30)
    connection_type: ConnectionType
    external_account_id: str | None = Field(default=None, max_length=500)
    email: EmailStr | None = None
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_username: str | None = Field(default=None, max_length=320)
    metadata: dict[str, object] = Field(default_factory=dict)


class CredentialUpload(BaseModel):
    """Receives secrets once; they are encrypted at rest and never returned."""

    api_key: str | None = Field(default=None, min_length=1, max_length=1000)
    smtp_password: str | None = Field(default=None, min_length=1, max_length=500)
    client_secret: str | None = Field(default=None, min_length=1, max_length=500)
    access_token: str | None = Field(default=None, min_length=1, max_length=5000)
    refresh_token: str | None = Field(default=None, min_length=1, max_length=5000)
    client_id: str | None = Field(default=None, max_length=500)
    smtp_username: str | None = Field(default=None, max_length=320)
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    tenant_id: str | None = Field(default=None, max_length=500)
    scopes: list[str] = Field(default_factory=list)
    expires_at: datetime | None = None

    @model_validator(mode="after")
    def require_at_least_one_secret(self) -> CredentialUpload:
        secrets = (
            self.api_key,
            self.smtp_password,
            self.client_secret,
            self.access_token,
            self.refresh_token,
        )
        if all(value is None for value in secrets):
            raise ValueError("At least one credential secret is required")
        return self


class SenderConnectionResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    provider: str
    connection_type: str
    status: str
    external_account_id: str | None
    email: EmailStr | None
    credential_configured: bool
    credential_version: str
    credential_expires_at: datetime | None
    metadata: dict[str, object]
    last_connected_at: datetime | None
    created_at: datetime
    updated_at: datetime


class CredentialStatusResponse(BaseModel):
    connection_id: UUID
    configured: bool
    credential_version: str
    expires_at: datetime | None


class SenderAccountCreate(BaseModel):
    email: EmailStr
    display_name: str | None = Field(default=None, max_length=200)
    external_sender_id: str | None = Field(default=None, max_length=500)


class WarmupSettingsUpdate(BaseModel):
    """Mutable warmup configuration for a sender account (Phase 10Q).

    All fields are optional for PATCH-style partial updates; the service layer
    validates cross-field constraints (e.g. ``minimum_delay <= maximum_delay``).
    """

    daily_limit: int | None = Field(default=None, ge=1, le=1000)
    reply_rate_target: float | None = Field(default=None, ge=0.0, le=1.0)
    start_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    end_time: str | None = Field(default=None, pattern=r"^([01]\d|2[0-3]):[0-5]\d$")
    weekdays: list[str] | None = None
    minimum_delay: int | None = Field(default=None, ge=0, le=86400)
    maximum_delay: int | None = Field(default=None, ge=0, le=604800)
    target_provider_distribution: dict[str, float] | None = None


class WarmupSettingsResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    sender_id: UUID
    enabled: bool
    daily_limit: int
    reply_rate_target: float
    start_time: str | None
    end_time: str | None
    weekdays: list[str]
    minimum_delay: int
    maximum_delay: int
    target_provider_distribution: dict[str, float]


class ReplySyncResponse(BaseModel):
    sender_id: UUID
    linked_replies: int


class SenderImportItem(BaseModel):
    email: EmailStr
    display_name: str | None = Field(default=None, max_length=200)
    external_sender_id: str | None = Field(default=None, max_length=500)


class SenderImportRequest(BaseModel):
    connection_id: UUID
    senders: list[SenderImportItem] = Field(min_length=1, max_length=500)


class SenderImportResponse(BaseModel):
    imported: int
    skipped: int
    accounts: list[SenderAccountResponse]
    errors: list[str]


class SenderAccountUpdate(BaseModel):
    status: SenderAccountStatus


class SenderAccountResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    connection_id: UUID
    email: EmailStr
    display_name: str | None
    provider: str
    external_sender_id: str | None
    status: str
    health_status: str
    campaign_enabled: bool
    warmup_enabled: bool
    reply_sync_enabled: bool
    daily_campaign_limit: int
    daily_warmup_limit: int
    last_success_at: datetime | None
    last_failure_at: datetime | None
    last_error: str | None
    last_used_at: datetime | None
    created_at: datetime
    updated_at: datetime


class DiscoveredSenderResponse(BaseModel):
    email: EmailStr
    display_name: str | None
    external_sender_id: str | None
    verified: bool


class DiscoveryResponse(BaseModel):
    provider: str
    supports_discovery: bool
    discovered: int
    senders: list[DiscoveredSenderResponse]
    errors: list[str]


class GoogleAuthorizeRequest(BaseModel):
    """Start the Google consent flow for an existing sender connection.

    The ``connection_id`` is written into the OAuth state; the callback never
    trusts the query string for tenant/identity — it comes from state.

    Set ``inbox_access`` to request the opt-in ``gmail.readonly`` scope for
    this connection (reply-sync). Requires a fresh consent round-trip; send-only
    connections keep the minimum send/identity scopes and can never read a
    mailbox.
    """

    connection_id: UUID
    inbox_access: bool = False


class GoogleAuthorizeResponse(BaseModel):
    authorization_url: str


class MicrosoftAuthorizeRequest(BaseModel):
    """Start the Microsoft 365 consent flow for an existing sender connection.

    The ``connection_id`` is written into the OAuth state; the callback never
    trusts the query string for tenant/identity — it comes from state.

    Set ``inbox_access`` to request the opt-in ``Mail.Read`` permission for
    this connection (reply-sync). Requires a fresh consent round-trip; send-only
    connections keep the minimum send permissions and can never read a mailbox.
    """

    connection_id: UUID
    inbox_access: bool = False


class MicrosoftAuthorizeResponse(BaseModel):
    authorization_url: str


class MicrosoftConnectionDetailsResponse(BaseModel):
    """Safe connection detail surface — never returns credentials."""

    id: UUID
    provider: str
    connection_type: str
    status: str
    external_account_id: str | None
    email: EmailStr | None
    created_at: datetime
    updated_at: datetime
    last_validated_at: datetime | None = None
    last_error_code: str | None = None
    last_error_at: datetime | None = None


class MicrosoftTestSendRequest(BaseModel):
    """Explicit, single-recipient test send through a CONNECTED Microsoft sender."""

    recipient: EmailStr = Field(max_length=320)
    subject: str = Field(default="Microsoft 365 connection test", max_length=255)
    text_body: str = Field(default="This is a test message.", max_length=10000)


class ZohoAuthorizeRequest(BaseModel):
    """Start the Zoho Mail consent flow for an existing sender connection.

    The ``connection_id`` is written into the OAuth state; the callback never
    trusts the query string for tenant/identity — it comes from state.
    """

    connection_id: UUID


class ZohoAuthorizeResponse(BaseModel):
    authorization_url: str


class ZohoConnectionDetailsResponse(BaseModel):
    """Safe connection detail surface — never returns credentials."""

    id: UUID
    provider: str
    connection_type: str
    status: str
    external_account_id: str | None
    email: EmailStr | None
    created_at: datetime
    updated_at: datetime
    last_validated_at: datetime | None = None
    last_error_code: str | None = None
    last_error_at: datetime | None = None


class ZohoTestSendRequest(BaseModel):
    """Explicit, single-recipient test send through a CONNECTED Zoho sender."""

    recipient: EmailStr = Field(max_length=320)
    subject: str = Field(default="Zoho Mail connection test", max_length=255)
    text_body: str = Field(default="This is a test message.", max_length=10000)


class SenderTestRequest(BaseModel):
    recipient: EmailStr = Field(max_length=320)


class SenderTestResponse(BaseModel):
    sender_id: UUID
    connection_id: UUID
    provider: str
    from_email: EmailStr | None
    recipient: EmailStr
    message_id: str
    sent_at: datetime