from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from pydantic import (
    BaseModel,
    ConfigDict,
    EmailStr,
    Field,
    ValidationInfo,
    field_validator,
    model_validator,
)

from app.utils.emails import normalize_email

CONTACT_STATUSES = {"ACTIVE", "INACTIVE", "UNSUBSCRIBED", "BOUNCED", "SUPPRESSED", "INVALID"}
CUSTOM_FIELD_TYPES = {"TEXT", "NUMBER", "BOOLEAN", "DATE", "SELECT", "MULTI_SELECT"}
CUSTOM_FIELD_KEY_PATTERN = "^[a-z][a-z0-9_]*$"


class ContactBase(BaseModel):
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    email: EmailStr
    phone: str | None = Field(default=None, max_length=50)
    company: str | None = Field(default=None, max_length=200)
    designation: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    website: str | None = Field(default=None, max_length=500)
    industry: str | None = Field(default=None, max_length=150)
    source: str | None = Field(default=None, max_length=100)
    source_reference: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=20_000)
    status: str = Field(default="ACTIVE", max_length=30)
    custom_fields: dict[str, str] = Field(default_factory=dict)
    tag_ids: list[UUID] | None = Field(default=None, max_length=500)
    list_ids: list[UUID] | None = Field(default=None, max_length=500)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr) -> str:
        return normalize_email(str(value))

    @field_validator("custom_fields")
    @classmethod
    def validate_custom_fields(cls, value: dict[str, str]) -> dict[str, str]:
        for key in value:
            if not key or not key.strip():
                raise ValueError("Custom field keys cannot be empty")
        return {str(key).strip(): str(item) for key, item in value.items()}

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str) -> str:
        value = value.upper()
        if value not in CONTACT_STATUSES:
            raise ValueError("Unsupported contact status")
        return value


class ContactCreate(ContactBase):
    pass


class ContactUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    first_name: str | None = Field(default=None, max_length=100)
    last_name: str | None = Field(default=None, max_length=100)
    email: EmailStr | None = None
    phone: str | None = Field(default=None, max_length=50)
    company: str | None = Field(default=None, max_length=200)
    designation: str | None = Field(default=None, max_length=200)
    location: str | None = Field(default=None, max_length=200)
    website: str | None = Field(default=None, max_length=500)
    industry: str | None = Field(default=None, max_length=150)
    source: str | None = Field(default=None, max_length=100)
    source_reference: str | None = Field(default=None, max_length=500)
    notes: str | None = Field(default=None, max_length=20_000)
    status: str | None = Field(default=None, max_length=30)
    custom_fields: dict[str, str] | None = None
    tag_ids: list[UUID] | None = Field(default=None, max_length=500)
    list_ids: list[UUID] | None = Field(default=None, max_length=500)

    @field_validator("email")
    @classmethod
    def normalize_email(cls, value: EmailStr | None) -> str | None:
        return normalize_email(str(value)) if value else None

    @field_validator("custom_fields")
    @classmethod
    def validate_custom_fields(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if value is not None:
            for key in value:
                if not key or not key.strip():
                    raise ValueError("Custom field keys cannot be empty")
            return {str(key).strip(): str(item) for key, item in value.items()}
        return value

    @field_validator("status")
    @classmethod
    def validate_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.upper()
        if value not in CONTACT_STATUSES:
            raise ValueError("Unsupported contact status")
        return value


class ContactResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    first_name: str | None
    last_name: str | None
    email: EmailStr
    phone: str | None
    company: str | None
    designation: str | None
    location: str | None
    website: str | None
    industry: str | None
    source: str | None
    source_reference: str | None
    notes: str | None
    status: str
    validation_status: str
    suppression_status: str
    unsubscribe_status: str
    custom_fields: dict[str, str]
    tags: list[str]
    list_ids: list[UUID]
    email_status: str
    email_type: str
    email_provider: str
    domain_status: str
    mx_status: str
    smtp_status: str
    disposable: bool
    role_account: bool
    catch_all: bool | None
    phone_status: str
    phone_type: str
    company_status: str
    duplicate_status: str
    duplicate_score: int | None
    verification_score: int | None
    risk_level: str
    verification_status: str
    last_verified_at: datetime | None
    verification_details: dict[str, Any] | None
    created_at: datetime
    updated_at: datetime


class VerificationJobResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    status: str
    scope: str
    total_count: int
    processed_count: int
    valid_count: int
    invalid_count: int
    risky_count: int
    needs_review_count: int
    duplicate_count: int
    unknown_count: int
    failed_count: int
    error_message: str | None
    started_at: datetime | None
    finished_at: datetime | None
    created_at: datetime


class SingleVerifyRequest(BaseModel):
    probe_smtp: bool = False


class BulkVerifyRequest(BaseModel):
    contact_ids: list[UUID] | None = Field(default=None, max_length=5000)
    probe_smtp: bool = False
    all_contacts: bool = False
    source: str | None = None
    status: str | None = None
    list_id: UUID | None = None

    @model_validator(mode="after")
    def _require_target(self) -> BulkVerifyRequest:
        if not self.all_contacts and not self.contact_ids and not any(
            (self.source, self.status, self.list_id)
        ):
            raise ValueError("Provide contact_ids, all_contacts, or at least one filter")
        return self


class ContactPage(BaseModel):
    items: list[ContactResponse]
    page: int
    page_size: int
    total: int


class BulkContactAction(BaseModel):
    contact_ids: list[UUID] = Field(min_length=1, max_length=500)
    action: str = Field(pattern="^(delete|tag|untag|list|unlist|status)$")
    target_id: UUID | None = None
    status: str | None = None

    @field_validator("status")
    @classmethod
    def validate_bulk_status(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.upper()
        if value not in CONTACT_STATUSES:
            raise ValueError("Unsupported contact status")
        return value


class BulkActionResult(BaseModel):
    affected: int
    skipped: int = 0
    failed: int = 0


class ContactListCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=500)


class ContactListUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=500)


class ContactMembershipRequest(BaseModel):
    contact_ids: list[UUID] = Field(min_length=1, max_length=500)


class ContactListResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    name: str
    description: str | None
    created_at: datetime
    updated_at: datetime


class ContactTagCreate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ContactTagUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=100)


class ContactTagResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    name: str
    created_at: datetime
    updated_at: datetime


class ContactFieldDefinitionCreate(BaseModel):
    model_config = ConfigDict(validate_default=True)
    key: str = Field(min_length=1, max_length=100, pattern=CUSTOM_FIELD_KEY_PATTERN)
    label: str = Field(min_length=1, max_length=150)
    field_type: str
    options: list[str] = Field(default_factory=list, max_length=200)
    required: bool = False

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        return value.strip().lower()

    @field_validator("field_type")
    @classmethod
    def validate_field_type(cls, value: str) -> str:
        value = value.upper()
        if value not in CUSTOM_FIELD_TYPES:
            raise ValueError(f"Unsupported custom field type: {value}")
        return value

    @field_validator("options")
    @classmethod
    def validate_options(cls, value: list[str], info: ValidationInfo) -> list[str]:
        options = [str(item).strip() for item in value if item is not None]
        field_type = str(info.data.get("field_type", ""))
        if field_type in {"SELECT", "MULTI_SELECT"} and not options:
            raise ValueError(f"{field_type} fields require at least one option")
        if field_type not in {"SELECT", "MULTI_SELECT"} and options:
            raise ValueError("Options are only allowed for SELECT and MULTI_SELECT fields")
        if len(set(options)) != len(options):
            raise ValueError("Custom field options must be unique")
        return options


class ContactFieldDefinitionUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str | None = Field(default=None, min_length=1, max_length=150)
    field_type: str | None = None
    options: list[str] | None = Field(default=None, max_length=200)
    required: bool | None = None

    @field_validator("field_type")
    @classmethod
    def validate_field_type(cls, value: str | None) -> str | None:
        if value is None:
            return None
        value = value.upper()
        if value not in CUSTOM_FIELD_TYPES:
            raise ValueError(f"Unsupported custom field type: {value}")
        return value

    @field_validator("options")
    @classmethod
    def validate_options(cls, value: list[str] | None, info: ValidationInfo) -> list[str] | None:
        if value is None:
            return None
        options = [str(item).strip() for item in value if item is not None]
        field_type = str(info.data.get("field_type", "") or "")
        if field_type in {"SELECT", "MULTI_SELECT"} and not options:
            raise ValueError(f"{field_type} fields require at least one option")
        if len(set(options)) != len(options):
            raise ValueError("Custom field options must be unique")
        return options


class ContactFieldDefinitionResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    key: str
    label: str
    field_type: str
    options: list[str]
    required: bool
    created_at: datetime
    updated_at: datetime
