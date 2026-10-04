from __future__ import annotations

from datetime import datetime
from typing import Any, Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, ValidationInfo, field_validator

SCALAR_CONTACT_FIELDS = {
    "first_name",
    "last_name",
    "email",
    "phone",
    "company",
    "designation",
    "location",
    "website",
    "industry",
    "source",
    "source_reference",
    "status",
}
OPERATORS = {
    "eq",
    "neq",
    "contains",
    "not_contains",
    "gt",
    "gte",
    "lt",
    "lte",
    "in",
    "not_in",
    "is_empty",
    "is_not_empty",
}
NUMERIC_OPERATORS = {"gt", "gte", "lt", "lte"}
LIST_OPERATORS = {"in", "not_in"}
EMPTY_OPERATORS = {"is_empty", "is_not_empty"}


class SegmentCondition(BaseModel):
    field: str = Field(min_length=1, max_length=120)
    operator: str
    value: str | int | float | list[str] | None = None

    @field_validator("field")
    @classmethod
    def validate_field(cls, value: str) -> str:
        value = value.strip()
        if value in SCALAR_CONTACT_FIELDS:
            return value
        if value.startswith("custom."):
            key = value[len("custom."):]
            if not key or not all(char.isascii() and (char.isalnum() or char == "_") for char in key):
                raise ValueError("Custom segment fields must be custom.<key> with a valid key")
            return value
        raise ValueError(f"Unsupported segment field: {value}")

    @field_validator("operator")
    @classmethod
    def validate_operator(cls, value: str) -> str:
        value = value.lower()
        if value not in OPERATORS:
            raise ValueError(f"Unsupported segment operator: {value}")
        return value

    @field_validator("value")
    @classmethod
    def validate_value(cls, value: Any, info: ValidationInfo) -> str | int | float | list[str] | None:
        operator = str(info.data.get("operator", ""))
        if operator in EMPTY_OPERATORS:
            return None
        if operator in LIST_OPERATORS:
            if not isinstance(value, list) or not value:
                raise ValueError(f"Operator {operator} requires a non-empty list of values")
            return [str(item).strip() for item in value]
        if value is None or (isinstance(value, (str, list)) and not value):
            raise ValueError(f"Operator {operator} requires a value")
        if operator in NUMERIC_OPERATORS:
            try:
                return float(value)
            except (TypeError, ValueError):
                raise ValueError(f"Operator {operator} requires a numeric value") from None
        return str(value)


class ContactSegmentFilter(BaseModel):
    match: Literal["all", "any"] = "all"
    conditions: list[SegmentCondition] = Field(min_length=1, max_length=50)


class ContactSegmentCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=500)
    filters: ContactSegmentFilter


class ContactSegmentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str | None = Field(default=None, min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=500)
    filters: ContactSegmentFilter | None = None


class ContactSegmentResponse(BaseModel):
    model_config = ConfigDict(from_attributes=True)
    id: UUID
    tenant_id: UUID
    name: str
    description: str | None
    filters: ContactSegmentFilter
    created_at: datetime
    updated_at: datetime