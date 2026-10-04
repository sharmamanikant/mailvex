from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    field_validator,
    model_validator,
)

ProviderType = Literal["GOOGLE", "MICROSOFT", "SMTP", "FUTURE_ESP"]
SenderStatus = Literal["CONNECTED", "REAUTH_REQUIRED", "DISABLED", "DISCONNECTED", "SUSPENDED", "HEALTH_WARNING", "HEALTH_CRITICAL"]
ConnectionStatus = Literal["CONNECTED", "REAUTH_REQUIRED", "DISCONNECTED", "SUSPENDED"]


class SenderCreate(BaseModel):
    email: EmailStr
    display_name: str | None = Field(default=None, max_length=200)
    reply_to: EmailStr | None = None
    provider: ProviderType
    timezone: str = Field(default="UTC", min_length=1, max_length=100)
    company: str | None = Field(default=None, max_length=200)
    designation: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    signature: str | None = Field(default=None, max_length=10_000)
    encrypted_credential_ref: str | None = Field(default=None, min_length=1, max_length=500)
    encryption_key_version: str | None = Field(default=None, min_length=1, max_length=50)
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_tls_mode: Literal["TLS", "STARTTLS", "SSL"] | None = None
    smtp_username: EmailStr | None = None
    smtp_password: str | None = Field(default=None, min_length=1, max_length=500)

    @field_validator("email", "reply_to")
    @classmethod
    def normalize_email(cls, value: EmailStr | None) -> str | None:
        return str(value).strip().lower() if value else None

    @model_validator(mode="after")
    def validate_smtp_configuration(self) -> SenderCreate:
        if self.provider == "SMTP" and (not self.smtp_host or not self.smtp_port or not self.smtp_tls_mode):
            raise ValueError("SMTP requires host, port, and TLS mode")
        return self


class SenderUpdate(BaseModel):
    email: EmailStr | None = None
    display_name: str | None = Field(default=None, max_length=200)
    reply_to: EmailStr | None = None
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    company: str | None = Field(default=None, max_length=200)
    designation: str | None = Field(default=None, max_length=200)
    phone: str | None = Field(default=None, max_length=50)
    signature: str | None = Field(default=None, max_length=10_000)
    smtp_host: str | None = Field(default=None, max_length=255)
    smtp_port: int | None = Field(default=None, ge=1, le=65535)
    smtp_tls_mode: Literal["TLS", "STARTTLS", "SSL"] | None = None
    smtp_username: EmailStr | None = None
    smtp_password: str | None = Field(default=None, min_length=1, max_length=500)


class SenderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    email: EmailStr
    display_name: str | None
    reply_to: EmailStr | None
    provider: str
    status: str
    connection_status: str = "CONNECTED"
    health_score: Decimal | None
    timezone: str
    smtp_host: str | None = None
    smtp_port: int | None = None
    smtp_tls_mode: str | None = None
    smtp_username: EmailStr | None = None
    company: str | None = None
    designation: str | None = None
    phone: str | None = None
    signature: str | None = None
    last_connected_at: datetime | None = None
    last_used_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class SenderHealthResponse(BaseModel):
    sender_id: UUID
    status: str
    health_score: Decimal | None
    healthy: bool


class SenderConnectionResponse(BaseModel):
    sender: SenderResponse
    provider_authenticated: bool


class TestEmailRequest(BaseModel):
    recipient: EmailStr


class SenderOperationResponse(BaseModel):
    sender_id: UUID
    successful: bool
    message: str


class ReconnectResponse(BaseModel):
    sender_id: UUID
    provider: str
    authorization_url: str
