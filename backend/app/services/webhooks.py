from __future__ import annotations

import hashlib
import hmac
import time
from collections.abc import Mapping
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Webhook


class WebhookVerificationError(ValueError):
    pass


class WebhookService:
    def __init__(self, session: Session, tenant_id: UUID, secrets: Mapping[str, str]) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.secrets = secrets

    def receive(self, provider: str, event_id: str, payload: dict[str, object], signature: str, timestamp: int) -> Webhook:
        existing = self.session.scalar(select(Webhook).where(Webhook.tenant_id == self.tenant_id, Webhook.provider == provider, Webhook.external_event_id == event_id))
        if existing is not None:
            if existing.signature_valid:
                return existing
            raise WebhookVerificationError("Webhook event was previously rejected")
        if abs(int(time.time()) - timestamp) > 300:
            raise WebhookVerificationError("Webhook timestamp is outside the replay window")
        secret = self.secrets.get(provider)
        if not secret:
            raise WebhookVerificationError("Webhook provider is not configured")
        expected = hmac.new(secret.encode(), f"{timestamp}.{event_id}".encode(), hashlib.sha256).hexdigest()
        valid = hmac.compare_digest(expected, signature)
        event = Webhook(tenant_id=self.tenant_id, provider=provider, external_event_id=event_id, signature_valid=valid, processing_status="VERIFIED" if valid else "FAILED", payload=payload)
        self.session.add(event)
        self.session.commit()
        if not valid:
            raise WebhookVerificationError("Webhook signature is invalid")
        return event
