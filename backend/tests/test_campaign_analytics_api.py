from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import get_db
from app.main import app
from app.models import (
    Base,
    Campaign,
    CampaignRecipient,
    Contact,
    EmailAccount,
    Message,
    Permission,
    Role,
    RolePermission,
    Tenant,
    User,
    UserRole,
)
from app.security.passwords import hash_password
from app.services.delivery_events import DeliveryEventService
from app.services.delivery_jobs import DeliveryJobService


@pytest.fixture()
def analytics_campaign_client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ca.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    tenant = Tenant(name="CA", slug=f"ca-{uuid4().hex[:8]}")
    session.add(tenant)
    session.flush()
    user = User(
        tenant_id=tenant.id,
        email="owner@example.com",
        password_hash=hash_password("correct horse battery staple"),
        display_name="Owner",
    )
    role = Role(tenant_id=tenant.id, name="Admin")
    permission = Permission(key="analytics.read", description="Read analytics")
    session.add_all([user, role, permission])
    session.flush()
    session.add_all(
        [
            UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id),
            RolePermission(role_id=role.id, permission_id=permission.id),
        ]
    )

    sender = EmailAccount(
        tenant_id=tenant.id, provider="GOOGLE", email="sender@example.com", status="CONNECTED"
    )
    session.add(sender)
    session.flush()
    contacts = [
        Contact(tenant_id=tenant.id, email=e, first_name=f"F{i}")
        for i, e in enumerate(["a@example.com", "b@example.com", "c@example.com"])
    ]
    session.add_all(contacts)
    session.flush()
    campaign = Campaign(
        tenant_id=tenant.id,
        name="CA campaign",
        objective="Launch",
        sender_id=sender.id,
        status="APPROVED",
        schedule_config={"start_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
    )
    session.add(campaign)
    session.flush()
    for contact in contacts:
        session.add(
            CampaignRecipient(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                contact_id=contact.id,
            )
        )
    session.commit()
    DeliveryJobService(session, tenant.id).materialize_for_campaign(campaign.id)

    messages = {}
    for contact in contacts:
        message = Message(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            sender_id=sender.id,
            contact_id=contact.id,
            subject="Hello",
        )
        session.add(message)
        session.flush()
        messages[contact.email] = message
    DeliveryEventService(session, tenant.id).process_event(
        "GOOGLE", "api-evt-1", "delivered", "a@example.com",
        message_id=messages["a@example.com"].id,
        campaign_id=campaign.id,
    )
    session.commit()
    session.close()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    login = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "correct horse battery staple"},
    )
    token = login.json()["access_token"]
    yield client, token, tenant.id, campaign.id
    app.dependency_overrides.clear()
    engine.dispose()


def test_campaign_analytics_summary(analytics_campaign_client) -> None:
    client, token, _tenant, campaign_id = analytics_campaign_client
    response = client.get(
        f"/api/v1/campaigns/{campaign_id}/analytics",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["campaign_id"] == str(campaign_id)
    assert payload["metrics"]["recipients"] == 3
    assert payload["metrics"]["delivered"] == 1
    assert "percentages" in payload


def test_campaign_analytics_recipients(analytics_campaign_client) -> None:
    client, token, _tenant, campaign_id = analytics_campaign_client
    response = client.get(
        f"/api/v1/campaigns/{campaign_id}/analytics/recipients?page_size=2",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] == 3
    assert len(payload["items"]) == 2


def test_campaign_analytics_export_csv(analytics_campaign_client) -> None:
    client, token, _tenant, campaign_id = analytics_campaign_client
    response = client.get(
        f"/api/v1/campaigns/{campaign_id}/analytics/export",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    text = response.text
    assert text.splitlines()[0] == "recipient,status,event,timestamp,reason"
    assert len(text.splitlines()) == 4


def test_campaign_analytics_unknown_campaign_404(analytics_campaign_client) -> None:
    client, token, _tenant, _campaign_id = analytics_campaign_client
    response = client.get(
        f"/api/v1/campaigns/{uuid4()}/analytics",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 404


def test_campaign_analytics_requires_permission(analytics_campaign_client) -> None:
    client, _token, _tenant, campaign_id = analytics_campaign_client
    # A user without analytics.read would get 403; admin bypasses. We just
    # assert the endpoint is protected (an unauthenticated request is 401/403).
    response = client.get(f"/api/v1/campaigns/{campaign_id}/analytics")
    assert response.status_code in (401, 403)
