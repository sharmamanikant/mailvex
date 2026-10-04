from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class ConversationResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    provider_thread_id: str
    sender_id: UUID | None
    contact_id: UUID | None
    campaign_id: UUID | None
    subject: str | None
    status: str
    assigned_user_id: UUID | None
    notes: str | None
    tags: list[str]
    last_message_at: datetime | None
    reply_count: int


class ReplyResponse(BaseModel):
    id: UUID
    message_id: UUID | None
    sender_email: str | None
    recipient_email: str | None
    body_text: str | None
    body_html: str | None
    classification: str | None
    suggested_action: str | None
    suggested_response: str | None
    approval_status: str | None
    received_at: datetime


class ConversationDetailResponse(BaseModel):
    conversation: ConversationResponse
    replies: list[ReplyResponse]


class ConversationUpdate(BaseModel):
    status: str | None = Field(default=None, pattern="^(OPEN|PENDING|INTERESTED|NOT_INTERESTED|CLOSED|UNSUBSCRIBED)$")
    assigned_user_id: UUID | None = None
    notes: str | None = Field(default=None, max_length=20_000)
    tags: list[str] | None = Field(default=None, max_length=50)


class ConversationReplyDraft(BaseModel):
    body_text: str = Field(min_length=1, max_length=50_000)


class ConversationReplyDecision(BaseModel):
    approved: bool = False
    send: bool = False
    edited_response: str | None = Field(default=None, max_length=50_000)
    automation_policy_enabled: bool = False
