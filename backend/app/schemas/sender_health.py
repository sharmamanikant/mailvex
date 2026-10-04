"""Pydantic schemas for the Phase 5 sender health endpoints.

These response models mirror the sanitized payloads produced by the health
engine: no credentials, tokens, or raw DNS payloads are ever serialized.
"""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from typing import Any
from uuid import UUID

from pydantic import BaseModel, Field


class SenderHealthCheckResultOut(BaseModel):
    id: UUID | None = None
    check_type: str
    status: str  # PASS | WARNING | FAIL | UNKNOWN | NOT_APPLICABLE
    score: Decimal | None = None
    severity: str = "INFO"  # INFO | LOW | MEDIUM | HIGH | CRITICAL
    title: str
    summary: str | None = None
    technical_details: str | None = None
    recommendation: str | None = None
    metadata: dict[str, Any] = Field(default_factory=dict)
    checked_at: datetime


class ScoreExplanationOut(BaseModel):
    version: str
    weights: dict[str, float]
    unknown_handling: str
    thresholds: dict[str, float]


class SenderHealthCheckOut(BaseModel):
    health_check_id: UUID
    sender_id: UUID
    tenant_id: UUID
    overall_status: str
    overall_score: Decimal | None = None
    score_version: str
    triggered_by: str  # MANUAL | SCHEDULED | SYSTEM
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    error_message: str | None = None
    results: list[SenderHealthCheckResultOut] = Field(default_factory=list)
    score_explanation: ScoreExplanationOut | None = None


class SenderHealthOverviewOut(BaseModel):
    id: UUID
    email: str
    provider: str
    health_status: str
    health_score: Decimal | None = None
    last_health_check_at: datetime | None = None
    latest: SenderHealthCheckOut | None = None
    summary: str | None = None
    domain_authentication_summary: list[SenderHealthCheckResultOut] = Field(default_factory=list)


class SenderHealthHistoryItemOut(BaseModel):
    health_check_id: UUID
    triggered_by: str
    overall_status: str
    overall_score: Decimal | None = None
    score_version: str
    started_at: datetime
    completed_at: datetime | None = None
    duration_ms: int | None = None
    error_code: str | None = None
    result_count: int = 0


class SenderHealthHistoryOut(BaseModel):
    sender_id: UUID
    items: list[SenderHealthHistoryItemOut] = Field(default_factory=list)
    page: int
    page_size: int
    total: int
    total_pages: int = 0