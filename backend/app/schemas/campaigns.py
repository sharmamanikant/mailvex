from __future__ import annotations

from datetime import datetime
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, Field

CampaignStatus = Literal["DRAFT", "REVIEW", "APPROVED", "SCHEDULED", "RUNNING", "PAUSED", "COMPLETED", "CANCELLED"]
ValidationLevel = Literal["PASS", "WARNING", "BLOCK"]


class CampaignCreate(BaseModel):
    name: str = Field(min_length=1, max_length=200)
    objective: str = Field(min_length=1, max_length=10_000)
    description: str | None = Field(default=None, max_length=2000)
    sender_id: UUID
    template_version_id: UUID | None = None
    recipient_list_id: UUID | None = None
    segment_id: UUID | None = None
    recipient_ids: list[UUID] = Field(default_factory=list, max_length=100_000)
    schedule_config: dict[str, object] = Field(default_factory=dict)
    timezone: str = Field(default="UTC", min_length=1, max_length=100)
    scheduled_at: datetime | None = None
    timezone_policy: str = Field(default="UTC", min_length=1, max_length=100)
    follow_up_policy: dict[str, object] = Field(default_factory=dict)
    variable_mapping: dict[str, str] = Field(default_factory=dict)


class CampaignUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=200)
    objective: str | None = Field(default=None, min_length=1, max_length=10_000)
    description: str | None = Field(default=None, max_length=2000)
    sender_id: UUID | None = None
    template_version_id: UUID | None = None
    recipient_list_id: UUID | None = None
    segment_id: UUID | None = None
    recipient_ids: list[UUID] | None = Field(default=None, max_length=100_000)
    schedule_config: dict[str, object] | None = None
    timezone: str | None = Field(default=None, max_length=100)
    scheduled_at: datetime | None = None
    timezone_policy: str | None = Field(default=None, max_length=100)
    follow_up_policy: dict[str, object] | None = None
    variable_mapping: dict[str, str] | None = None


class CampaignResponse(BaseModel):
    id: UUID
    tenant_id: UUID
    name: str
    objective: str
    description: str | None
    sender_id: UUID
    template_version_id: UUID | None
    recipient_list_id: UUID | None
    segment_id: UUID | None
    timezone: str
    scheduled_at: datetime | None
    status: str
    schedule_config: dict[str, object]
    follow_up_policy: dict[str, object]
    variable_mapping: dict[str, str]
    recipient_count: int
    approved_by_id: UUID | None
    approved_at: datetime | None
    compliance_status: str | None = None
    compliance_reasons: list[str] = []
    compliance_evaluated_at: datetime | None = None
    created_at: datetime
    updated_at: datetime


class ValidationCheckResponse(BaseModel):
    name: str
    outcome: Literal["PASS", "WARNING", "BLOCK"]
    message: str
    remediation: str | None = None
    code: str | None = None


class CampaignValidationResponse(BaseModel):
    campaign_id: UUID
    level: ValidationLevel
    compliance_status: str | None = None
    compliance_reasons: list[str] = []
    checks: list[ValidationCheckResponse]


class CampaignDetailResponse(BaseModel):
    campaign: CampaignResponse
    recipient_ids: list[UUID]
    latest_version: dict[str, object] | None
    template_content: dict[str, object] | None = None
    recipient_preview: dict[str, str] = {}


class CampaignPreviewResponse(BaseModel):
    campaign_id: UUID
    subject: str
    html_body: str
    text_body: str
    missing_variables: list[str]
    warnings: list[str]


class StatusChangeRequest(BaseModel):
    status: CampaignStatus


class CampaignSenderAddRequest(BaseModel):
    sender_id: UUID
    daily_limit: int | None = Field(default=None, ge=1)
    enabled: bool = True


class CampaignSenderUpdateRequest(BaseModel):
    daily_limit: int | None = Field(default=None, ge=1)
    enabled: bool | None = None


class CampaignSenderResponse(BaseModel):
    id: UUID
    campaign_id: UUID
    sender_id: UUID
    email: str
    provider: str
    health_status: str
    daily_limit: int | None
    enabled: bool
    created_at: datetime
    updated_at: datetime
