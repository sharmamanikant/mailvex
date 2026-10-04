from __future__ import annotations

from uuid import UUID

from pydantic import BaseModel


class ComplianceCheckResponse(BaseModel):
    name: str
    outcome: str
    message: str
    remediation: str | None


class ComplianceResultResponse(BaseModel):
    campaign_id: UUID
    recipient_id: UUID
    outcome: str
    checks: list[ComplianceCheckResponse]
