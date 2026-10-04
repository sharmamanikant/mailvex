from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

GENERATION_TYPES = (
    "INITIAL_EMAIL",
    "FOLLOW_UP",
    "MEETING_REQUEST",
    "SERVICE_INTRO",
    "JOB_REQUIREMENT",
    "CANDIDATE_INTRO",
    "INFO_REQUEST",
    "EVENT_INVITATION",
    "THANK_YOU",
    "GENERAL_OUTREACH",
)
TONES = (
    "PROFESSIONAL",
    "FRIENDLY",
    "CONCISE",
    "CONSULTATIVE",
    "FORMAL",
    "TECHNICAL",
    "RECRUITMENT",
    "SALES",
    "NEUTRAL",
)
DESIRED_LENGTHS = ("SHORT", "MEDIUM", "LONG")
GENERATION_STATUSES = (
    "DRAFT",
    "GENERATING",
    "GENERATED",
    "REVIEW_REQUIRED",
    "APPROVED",
    "REJECTED",
    "FAILED",
)
TRANSFORM_ACTIONS = (
    "regenerate",
    "improve",
    "shorten",
    "expand",
    "change_tone",
    "translate",
)


class DraftGenerateRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=10_000)
    audience: str = Field(default="", max_length=5_000)
    context: str = Field(default="", max_length=20_000)
    service: str = Field(default="", max_length=1_000)
    product: str = Field(default="", max_length=1_000)
    cta: str = Field(default="", max_length=2_000)
    tone: str = Field(default="PROFESSIONAL", max_length=40)
    language: str = Field(default="English", max_length=100)
    desired_length: str = Field(default="MEDIUM", max_length=20)
    generation_type: str = Field(default="INITIAL_EMAIL", max_length=40)
    sender: dict[str, str] = Field(default_factory=dict)
    recipient: dict[str, str] = Field(default_factory=dict)
    custom_values: dict[str, str] = Field(default_factory=dict)
    sender_id: UUID | None = Field(default=None)
    contact_id: UUID | None = Field(default=None)
    template_id: UUID | None = Field(default=None)

    @field_validator("tone")
    @classmethod
    def validate_tone(cls, value: str) -> str:
        normalized = (value or "").strip().upper().replace(" ", "_")
        if normalized not in TONES:
            raise ValueError(f"Unsupported tone: {value}")
        return normalized

    @field_validator("desired_length")
    @classmethod
    def validate_length(cls, value: str) -> str:
        normalized = (value or "").strip().upper()
        if normalized not in DESIRED_LENGTHS:
            raise ValueError(f"Unsupported desired length: {value}")
        return normalized

    @field_validator("generation_type")
    @classmethod
    def validate_generation_type(cls, value: str) -> str:
        normalized = (value or "").strip().upper().replace(" ", "_")
        if normalized not in GENERATION_TYPES:
            raise ValueError(f"Unsupported generation type: {value}")
        return normalized


class DraftUpdate(BaseModel):
    subject: str | None = Field(default=None, min_length=1, max_length=998)
    body: str | None = Field(default=None, min_length=1, max_length=1_000_000)


class DraftTransformRequest(BaseModel):
    action: str = Field(min_length=1, max_length=30)
    tone: str | None = Field(default=None, max_length=40)
    language: str | None = Field(default=None, max_length=100)

    @field_validator("action")
    @classmethod
    def validate_action(cls, value: str) -> str:
        if (value or "").strip().lower() not in TRANSFORM_ACTIONS:
            raise ValueError(f"Unsupported action: {value}")
        return value.strip().lower()

    @field_validator("tone")
    @classmethod
    def validate_tone(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().upper().replace(" ", "_")
        if normalized not in TONES:
            raise ValueError(f"Unsupported tone: {value}")
        return normalized


class DraftApproveRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class DraftRejectRequest(BaseModel):
    note: str | None = Field(default=None, max_length=1000)


class DraftSaveTemplateRequest(BaseModel):
    template_name: str = Field(min_length=1, max_length=150)


class DraftUsageResponse(BaseModel):
    provider: str | None
    model: str | None
    monthly_generations: int
    monthly_cost_usd: Decimal
    monthly_budget_usd: Decimal
    monthly_generation_limit: int


class AIMessageDraftResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    created_by_id: UUID | None = None
    contact_id: UUID | None = None
    campaign_id: UUID | None = None
    template_id: UUID | None = None
    approved_template_id: UUID | None = None
    objective: str
    input_context: dict[str, object]
    generated_subject: str
    generated_body: str
    generation_status: str
    generation_type: str
    tone: str
    language: str
    desired_length: str
    cta: str
    provider: str
    model: str | None
    generation_method: str
    warnings: list[str]
    input_tokens: int | None = None
    output_tokens: int | None = None
    total_tokens: int | None = None
    estimated_cost: Decimal | None = None
    cost_currency: str
    request_duration_ms: int | None = None
    error_message: str | None = None
    review_note: str | None = None
    reviewed_at: datetime | None = None
    approved_at: datetime | None = None
    approved_by_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class AIMessageDraftListResponse(BaseModel):
    items: list[AIMessageDraftResponse]
    total: int