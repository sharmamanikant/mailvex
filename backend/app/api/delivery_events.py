from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, Header, HTTPException
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.observability.metrics import get_metrics
from app.services.delivery_events import _PROVIDER_CANONICAL, DeliveryEventService
from app.services.webhooks import WebhookService, WebhookVerificationError

router = APIRouter(tags=["delivery-events"])
_metrics = get_metrics()


def _parse_datetime(value: Any) -> datetime | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(float(value), tz=UTC)
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _extract_recipient(data: Any) -> str:
    if isinstance(data, str):
        return data
    if isinstance(data, dict):
        for key in ("email", "recipient", "address", "to"):
            value = data.get(key)
            if isinstance(value, str):
                return value
    return ""


@router.post("/delivery-events/{provider}/{tenant_id}")
def receive_delivery_event(
    provider: str,
    tenant_id: UUID,
    payload: dict[str, Any],
    event_id: str = Header(...),
    timestamp: int = Header(...),
    signature: str = Header(...),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    """Provider webhook: verify signature, then normalize + process idempotently.

    Pipeline: signature verification -> timestamp/replay validation -> event id
    -> idempotency (unique provider_event_id) -> persist -> apply side effects.
    Unverified webhooks are rejected and never modify message state.
    """
    provider_key = provider.upper()
    started = time.perf_counter()
    try:
        WebhookService(session, tenant_id, settings.webhook_secrets).receive(
            provider_key,
            event_id,
            payload,
            signature,
            timestamp,
        )
    except WebhookVerificationError as exc:
        _metrics.counter("webhooks.received", provider=provider_key, outcome="verification_failed")
        _metrics.record_latency("webhooks.latency", time.perf_counter() - started, outcome="verification_failed")
        raise HTTPException(status_code=400, detail=str(exc)) from None

    _metrics.counter("webhooks.received", provider=provider_key, outcome="verified")
    _metrics.record_latency("webhooks.latency", time.perf_counter() - started, outcome="verified")

    event_type = str(payload.get("type") or "")
    recipient = _extract_recipient(payload.get("recipient") or payload.get("email") or "")
    if not recipient:
        raise HTTPException(status_code=400, detail="Recipient is missing from the event payload")
    message_id_value = payload.get("message_id")
    message_id: UUID | None = None
    if message_id_value:
        try:
            message_id = UUID(str(message_id_value))
        except ValueError:
            message_id = None

    campaign_id_value = payload.get("campaign_id")
    campaign_id: UUID | None = None
    if campaign_id_value:
        try:
            campaign_id = UUID(str(campaign_id_value))
        except ValueError:
            campaign_id = None

    raw_reference = payload.get("reference") or payload.get("diagnostic")
    service = DeliveryEventService(session, tenant_id)
    record = service.process_event(
        provider_key,
        event_id,
        event_type,
        recipient,
        message_id=message_id,
        event_time=_parse_datetime(payload.get("timestamp") or payload.get("event_time")),
        raw_reference=str(raw_reference) if raw_reference else None,
        campaign_id=campaign_id,
    )
    return {
        "event_id": str(record.id),
        "provider_event_id": record.provider_event_id,
        "event_type": record.event_type,
        "processed": record.processed_at is not None,
    }


@router.get("/delivery-events/types")
def delivery_event_types() -> dict[str, Any]:
    """Public enum of supported canonical event types and provider labels."""
    return {"types": ["DELIVERED", "TEMPORARY_FAILURE", "HARD_BOUNCE", "COMPLAINT", "UNSUBSCRIBED", "UNKNOWN"], "providers": {key: sorted(value) for key, value in _PROVIDER_CANONICAL.items()}}
