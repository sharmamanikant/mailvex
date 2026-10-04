"""Pydantic schemas for provider connections (Phase 1 provider foundation).

Responses never expose credential references, access tokens, refresh tokens or
client secrets - only a ``credential_configured`` flag plus non-sensitive
connection metadata.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, Field

Provider = Literal["GOOGLE", "MICROSOFT", "SENDGRID", "ZOHO", "SMTP"]
ProviderConnectionType = Literal["OAUTH", "API_KEY", "SMTP"]
ProviderConnectionStatus = Literal["CONNECTING", "CONNECTED", "ERROR", "DISCONNECTED", "REVOKED"]


class ProviderConnectionResponse(BaseModel):
    id: UUID
    provider: Provider
    connection_type: ProviderConnectionType
    provider_account_id: str | None = None
    workspace_domain: str | None = None
    display_name: str | None = None
    status: ProviderConnectionStatus
    scopes: list[str] = Field(default_factory=list)
    credential_configured: bool = False
    credential_expires_at: datetime | None = None
    connected_by: UUID | None = None
    last_sync_at: datetime | None = None
    last_sync_status: str | None = None
    last_sync_error: str | None = None
    last_sync_started_at: datetime | None = None
    last_sync_completed_at: datetime | None = None
    # Non-secret provider metadata (e.g. Microsoft 365 organization name,
    # default domain, masked tenant id). Never contains credentials.
    provider_metadata: dict[str, Any] = Field(default_factory=dict)
    # Counts from the most recent mailbox sync run.
    last_sync_stats: dict[str, Any] = Field(default_factory=dict)
    created_at: datetime
    updated_at: datetime


class GoogleConnectResponse(BaseModel):
    """Server-controlled OAuth start: the frontend only receives the URL."""

    authorization_url: str


class ProviderConnectionDisconnectResponse(BaseModel):
    id: UUID
    status: ProviderConnectionStatus