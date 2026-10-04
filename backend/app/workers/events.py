from __future__ import annotations

from uuid import UUID

from app.core.database import SessionLocal
from app.services.suppression import SuppressionService


def process_bounce(tenant_id: UUID, message_id: UUID, classification: str, diagnostic: str | None = None) -> str:
    with SessionLocal() as session:
        return SuppressionService(session, tenant_id).process_bounce(message_id, classification, diagnostic)


def process_complaint(tenant_id: UUID, message_id: UUID, provider_reference: str | None = None) -> None:
    with SessionLocal() as session:
        SuppressionService(session, tenant_id).process_complaint(message_id, provider_reference)