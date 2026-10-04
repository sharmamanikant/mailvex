from __future__ import annotations

import hashlib
import hmac
import time
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import get_db
from app.main import app
from app.models import (
    Base,
    Campaign,
    Contact,
    EmailAccount,
    Message,
    Tenant,
)


@pytest.fixture()
def webhook_api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'webhook_flow.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    tenant = Tenant(name="Webhook Flow Tenant", slug=f"wf-{uuid4().hex[:8]}")
    session.add(tenant)
    session.flush()
    sender = EmailAccount(tenant_id=tenant.id, provider="GOOGLE", email="sender@example.com", status="CONNECTED")
    contact = Contact(tenant_id=tenant.id, email="recipient@example.com", validation_status="VALID")
    session.add_all([sender, contact])
    session.flush()
    campaign = Campaign(tenant_id=tenant.id, name="Webhook campaign", objective="Test", sender_id=sender.id, status="APPROVED", schedule_config={})
    session.add(campaign)
    session.flush()
    message = Message(tenant_id=tenant.id, campaign_id=campaign.id, sender_id=sender.id, contact_id=contact.id, subject="Hi")
    session.add(message)
    session.commit()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    secret = "secret"
    object.__setattr__(settings, "webhook_secrets", {"GOOGLE": secret})
    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    def sign(event_id: str) -> tuple[int, str]:
        timestamp = int(time.time())
        signature = hmac.new(secret.encode(), f"{timestamp}.{event_id}".encode(), hashlib.sha256).hexdigest()
        return timestamp, signature

    yield client, tenant.id, contact, campaign, message, sign
    app.dependency_overrides.clear()
    engine.dispose()


def test_verified_webhook_normalizes_and_suppresses(webhook_api) -> None:
    client, tenant_id, contact, _campaign, message, sign = webhook_api
    timestamp, signature = sign("wf-hard-1")
    response = client.post(
        f"/api/v1/delivery-events/GOOGLE/{tenant_id}",
        headers={"event-id": "wf-hard-1", "timestamp": str(timestamp), "signature": signature},
        json={"type": "bounce", "recipient": {"email": contact.email}, "message_id": str(message.id), "campaign_id": str(message.campaign_id), "reference": "550 5.1.1"},
    )
    assert response.status_code == 200, response.text
    assert response.json()["event_type"] == "HARD_BOUNCE"
    assert response.json()["processed"] is True


def test_verified_webhook_is_idempotent(webhook_api) -> None:
    client, tenant_id, contact, _campaign, message, sign = webhook_api
    timestamp, signature = sign("wf-comp-1")
    url = f"/api/v1/delivery-events/GOOGLE/{tenant_id}"
    headers = {"event-id": "wf-comp-1", "timestamp": str(timestamp), "signature": signature}
    body = {"type": "complaint", "recipient": contact.email, "message_id": str(message.id), "campaign_id": str(message.campaign_id)}
    first = client.post(url, headers=headers, json=body)
    second = client.post(url, headers=headers, json=body)
    assert first.status_code == 200 and second.status_code == 200
    assert first.json()["event_id"] == second.json()["event_id"]


def test_unverified_webhook_is_rejected(webhook_api) -> None:
    client, tenant_id, contact, _campaign, _message, _sign = webhook_api
    timestamp = int(time.time())
    response = client.post(
        f"/api/v1/delivery-events/GOOGLE/{tenant_id}",
        headers={"event-id": "wf-bad-1", "timestamp": str(timestamp), "signature": "deadbeef"},
        json={"type": "bounce", "recipient": contact.email},
    )
    assert response.status_code == 400
    assert "signature" in response.text.lower()


def test_unconfigured_provider_webhook_is_rejected(webhook_api) -> None:
    client, tenant_id, contact, _campaign, _message, _sign = webhook_api
    timestamp, signature = _sign("wf-x-1")
    response = client.post(
        f"/api/v1/delivery-events/SERVICENOW/{tenant_id}",
        headers={"event-id": "wf-x-1", "timestamp": str(timestamp), "signature": signature},
        json={"type": "delivered", "recipient": contact.email},
    )
    assert response.status_code == 400


def test_delivery_event_types_enum(webhook_api) -> None:
    client, _tenant_id, _contact, _campaign, _message, _sign = webhook_api
    response = client.get("/api/v1/delivery-events/types")
    assert response.status_code == 200
    assert "HARD_BOUNCE" in response.json()["types"]
    assert "GOOGLE" in response.json()["providers"]
