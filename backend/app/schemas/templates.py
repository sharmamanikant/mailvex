from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

RECIPIENT_VARIABLES: list[str] = [
    "first_name",
    "last_name",
    "full_name",
    "email",
    "phone",
    "company",
    "designation",
    "location",
    "website",
    "industry",
    "source",
    "source_reference",
    "notes",
    "skills",
    "job_title",
    "experience",
    "requirement",
    "service",
    "service_area",
    "technology",
    "availability",
]

SENDER_VARIABLES: list[str] = [
    "sender_name",
    "sender_email",
    "sender_company",
    "sender_designation",
    "sender_phone",
    "sender_signature",
]

BUILT_IN_VARIABLES: set[str] = set(RECIPIENT_VARIABLES).union(SENDER_VARIABLES)


class TemplateCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=500)
    subject_template: str = Field(min_length=1, max_length=998)
    html_body: str = Field(min_length=1, max_length=1_000_000)
    text_body: str | None = Field(default=None, max_length=1_000_000)
    custom_variables: list[str] = Field(default_factory=list, max_length=100)


class TemplateUpdate(BaseModel):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=500)
    subject_template: str | None = Field(default=None, min_length=1, max_length=998)
    html_body: str | None = Field(default=None, min_length=1, max_length=1_000_000)
    text_body: str | None = Field(default=None, max_length=1_000_000)
    custom_variables: list[str] | None = Field(default=None, max_length=100)


class TemplatePreviewRequest(BaseModel):
    recipient: dict[str, str] = Field(default_factory=dict)
    sender: dict[str, str] = Field(default_factory=dict)
    custom_values: dict[str, str] = Field(default_factory=dict)
    sender_id: UUID | None = Field(default=None)


class TemplateRecipientPreviewRequest(BaseModel):
    sender_id: UUID | None = Field(default=None)
    sender: dict[str, str] = Field(default_factory=dict)
    custom_values: dict[str, str] = Field(default_factory=dict)


class RenderedTemplate(BaseModel):
    subject: str
    html_body: str
    text_body: str
    used_variables: list[str]
    missing_variables: list[str]
    warnings: list[str]


class TemplateVariablesResponse(BaseModel):
    recipient: list[str]
    sender: list[str]


class TemplateResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    name: str
    description: str | None
    status: str
    current_version_id: UUID | None = None
    created_by_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class TemplateVersionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    template_id: UUID
    version_number: int
    subject_template: str
    html_body: str
    text_body: str | None
    status: str
    variable_manifest: list[str]
    created_by_id: UUID | None = None
    created_at: datetime
    updated_at: datetime


class TemplateDetail(BaseModel):
    template: TemplateResponse
    version: TemplateVersionResponse


class TemplateListResponse(BaseModel):
    items: list[TemplateDetail]
    total: int


class TemplateVersionListResponse(BaseModel):
    items: list[TemplateVersionResponse]
    total: int