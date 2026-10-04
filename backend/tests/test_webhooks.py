from __future__ import annotations

import hashlib
import hmac
import time
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Tenant, Webhook
from app.services.webhooks import WebhookService, WebhookVerificationError


def test_webhook_requires_valid_signature_and_is_idempotent(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'webhooks.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Webhook Tenant", slug=f"webhook-{uuid4().hex[:8]}")
        session.add(tenant)
        session.commit()
        service = WebhookService(session, tenant.id, {"smtp": "secret"})
        timestamp = int(time.time())
        signature = hmac.new(b"secret", f"{timestamp}.event-1".encode(), hashlib.sha256).hexdigest()
        event = service.receive("smtp", "event-1", {"type": "accepted"}, signature, timestamp)
        assert event.processing_status == "VERIFIED"
        assert service.receive("smtp", "event-1", {"type": "accepted"}, signature, timestamp).id == event.id


def test_webhook_rejects_invalid_signature(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'webhooks-invalid.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Webhook Tenant", slug=f"webhook-{uuid4().hex[:8]}")
        session.add(tenant)
        session.commit()
        with pytest.raises(WebhookVerificationError):
            WebhookService(session, tenant.id, {"smtp": "secret"}).receive("smtp", "event-1", {}, "bad", int(time.time()))
        assert session.query(Webhook).one().processing_status == "FAILED"


def test_rejected_webhook_id_cannot_be_replayed(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'webhooks-replay.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Webhook Tenant", slug=f"webhook-{uuid4().hex[:8]}")
        session.add(tenant)
        session.commit()
        service = WebhookService(session, tenant.id, {"smtp": "secret"})
        with pytest.raises(WebhookVerificationError):
            service.receive("smtp", "event-1", {}, "bad", int(time.time()))
        timestamp = int(time.time())
        signature = hmac.new(b"secret", f"{timestamp}.event-1".encode(), hashlib.sha256).hexdigest()
        with pytest.raises(WebhookVerificationError, match="previously rejected"):
            service.receive("smtp", "event-1", {}, signature, timestamp)
