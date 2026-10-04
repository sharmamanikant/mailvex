"""Pydantic schemas for workspace mailbox discovery (Phase 2).

Responses never expose credential references, OAuth tokens, or any
provider secrets. The mailbox response contains only normalized profile
data from the directory provider.
"""

from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel

ProviderMailboxUserType = Literal["USER", "ALIAS", "GROUP", "OTHER"]
ProviderMailboxStatus = Literal["ACTIVE", "SUSPENDED", "DELETED", "UNKNOWN"]
SyncStatus = Literal["IDLE", "SYNCING", "COMPLETED", "FAILED"]


class MailboxResponse(BaseModel):
    id: UUID
    provider_connection_id: UUID
    provider_mailbox_id: str
    email: str
    display_name: str | None = None
    first_name: str | None = None
    last_name: str | None = None
    department: str | None = None
    job_title: str | None = None
    user_type: str
    provider_status: str
    is_suspended: bool = False
    is_deleted: bool = False
    last_discovered_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class MailboxPage(BaseModel):
    items: list[MailboxResponse]
    page: int
    page_size: int
    total: int


class MailboxSyncStartResponse(BaseModel):
    """Returned when a sync has been successfully enqueued."""

    connection_id: UUID
    sync_status: SyncStatus


class MailboxCountSummary(BaseModel):
    """Aggregate mailbox counts for a connection."""

    total: int = 0
    active: int = 0
    suspended: int = 0
    deleted: int = 0
