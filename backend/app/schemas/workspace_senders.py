"""Pydantic schemas for Phase 3 Sender records.

Senders are application-level sending identities derived from eligible
workspace mailboxes (a Sender always maps to exactly one Mailbox inside one
ProviderConnection). These models never expose credentials, tokens, or any
provider secret - only normalized identity + lifecycle state.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

SenderStatus = Literal["ACTIVE", "DISABLED", "ERROR", "REVOKED", "REMOVED"]
SenderHealthStatus = Literal["UNKNOWN", "HEALTHY", "WARNING", "CRITICAL", "CHECKING"]
SenderSelectionMode = Literal["EXPLICIT", "ALL_ELIGIBLE"]


class SenderAvailability(BaseModel):
    """Effective availability of a Sender for campaign sending.

    ``available`` is the single source of truth used by the future email
    engine before delivery. It is derived from tenant ownership plus the
    Sender status, the sendingEnabled flag, the Mailbox status and the
    ProviderConnection status — never from any single field alone.
    """

    available: bool = False
    sending_enabled: bool = False
    sender_status: SenderStatus = "ACTIVE"
    mailbox_status: str = "ACTIVE"
    provider_connection_status: str = "CONNECTED"
    reason: str | None = None


class SenderResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)

    id: UUID
    tenant_id: UUID
    mailbox_id: UUID
    provider_connection_id: UUID
    email: str
    display_name: str | None = None
    provider: str
    status: SenderStatus = "ACTIVE"
    sending_enabled: bool = False
    health_status: SenderHealthStatus = "UNKNOWN"
    health_score: Decimal | None = None
    last_health_check_at: datetime | None = None
    availability: SenderAvailability = SenderAvailability()
    created_at: datetime
    updated_at: datetime


class SenderMailboxSummary(BaseModel):
    """Safe mailbox information for the sender detail view (no credentials)."""

    id: UUID
    email: str
    display_name: str | None = None
    department: str | None = None
    job_title: str | None = None
    status: str = "ACTIVE"
    is_suspended: bool = False
    is_deleted: bool = False
    last_discovered_at: datetime | None = None


class SenderProviderConnectionSummary(BaseModel):
    """Safe provider connection information (no credentials ever)."""

    id: UUID
    provider: str
    status: str
    workspace_domain: str | None = None
    connection_type: str | None = None
    last_sync_status: str | None = None
    last_sync_completed_at: datetime | None = None


class SenderHealthInfo(BaseModel):
    """Phase 5 placeholder. No health score is fabricated in Phase 4."""

    status: SenderHealthStatus = "UNKNOWN"
    score: Decimal | None = None
    last_checked_at: datetime | None = None


class SenderDetailResponse(BaseModel):
    """Full sender detail: identity + effective availability + related state."""

    id: UUID
    tenant_id: UUID
    mailbox_id: UUID
    provider_connection_id: UUID
    email: str
    display_name: str | None = None
    provider: str
    status: SenderStatus = "ACTIVE"
    sending_enabled: bool = False
    availability: SenderAvailability
    mailbox: SenderMailboxSummary | None = None
    provider_connection: SenderProviderConnectionSummary | None = None
    health: SenderHealthInfo
    created_at: datetime
    updated_at: datetime


class SenderPage(BaseModel):
    items: list[SenderResponse]
    page: int
    page_size: int
    total: int
    total_pages: int = 0


class SenderBulkCreateRequest(BaseModel):
    selection_mode: SenderSelectionMode = "EXPLICIT"
    mailbox_ids: list[UUID] = Field(default_factory=list, max_length=1_000)

    def normalized_mailbox_ids(self) -> list[UUID]:
        """Deduplicate the requested mailbox ids while preserving order."""
        seen: set[UUID] = set()
        result: list[UUID] = []
        for mailbox_id in self.mailbox_ids:
            if mailbox_id not in seen:
                seen.add(mailbox_id)
                result.append(mailbox_id)
        return result


class SenderBulkCreateResponse(BaseModel):
    """Result of a bulk sender creation request.

    ``mode`` distinguishes synchronous completion (summary counts + created
    senders) from a background job (``request_id`` + ``status`` = QUEUED) for
    very large selections.
    """

    mode: Literal["SYNC", "ASYNC"] = "SYNC"
    created: int = 0
    already_exists: int = 0
    skipped: int = 0
    failed: int = 0
    total_attempted: int = 0
    senders: list[SenderResponse] = Field(default_factory=list)
    request_id: UUID | None = None
    status: str | None = None
    selection_mode: SenderSelectionMode = "EXPLICIT"


class SenderPatchRequest(BaseModel):
    sending_enabled: bool | None = None
    display_name: str | None = Field(default=None, max_length=200)


class SenderOperationResponse(BaseModel):
    sender_id: UUID
    status: str
    message: str