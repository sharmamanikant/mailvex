from __future__ import annotations

from datetime import datetime
from uuid import UUID

from pydantic import BaseModel, Field


class GenerateEmailRequest(BaseModel):
    objective: str = Field(min_length=1, max_length=10_000)
    audience: str = Field(min_length=1, max_length=5_000)
    context: str = Field(default="", max_length=20_000)
    service_product: str = Field(default="", max_length=1_000)
    tone: str = Field(default="professional", max_length=100)
    language: str = Field(default="English", max_length=100)
    cta: str = Field(default="", max_length=2_000)
    sender: dict[str, str] = Field(default_factory=dict)
    recipient: dict[str, str] = Field(default_factory=dict)
    custom_values: dict[str, str] = Field(default_factory=dict)
    instruction: str = Field(default="generate", pattern="^(generate|regenerate|shorten|expand|professional|friendly|translate|change_tone)$")


class AIGenerationResponse(BaseModel):
    id: UUID
    workflow_status: str
    subject: str
    body: str
    cta: str
    personalization_suggestions: list[str]
    follow_up: list[dict[str, str]]
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    estimated_cost: float
    cost_currency: str
    created_at: datetime


class GenerationTransformRequest(BaseModel):
    instruction: str = Field(pattern="^(regenerate|shorten|expand|professional|friendly|translate|change_tone)$")
    tone: str | None = Field(default=None, max_length=100)
    language: str | None = Field(default=None, max_length=100)
