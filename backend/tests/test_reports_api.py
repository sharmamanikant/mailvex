from __future__ import annotations

from datetime import UTC, datetime
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
    DeliveryJob,
    EmailAccount,
    Message,
    NormalizedDeliveryEvent,
    Permission,
    Role,
    RolePermission,
    Tenant,
    User,
    UserRole,
)
from app.security.passwords import hash_password


@pytest.fixture()
def reports_client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'reports_api.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    tenant = Tenant(name="R", slug=f"r-{uuid4().hex[:8]}")
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
        tenant_id=tenant.id,
        provider="GOOGLE",
        email="sender@example.com",
        status="CONNECTED",
        health_score=90,
    )
    session.add(sender)
    session.flush()

    contacts = [
        Contact(tenant_id=tenant.id, email=e, status=status)
        for e, status in [
            ("a@example.com", "ACTIVE"),
            ("b@example.com", "ACTIVE"),
            ("c@example.com", "UNSUBSCRIBED"),
        ]
    ]
    session.add_all(contacts)
    session.flush()

    campaign = Campaign(
        tenant_id=tenant.id,
        name="Launch campaign",
        objective="Launch",
        sender_id=sender.id,
        status="COMPLETED",
    )
    session.add(campaign)
    session.flush()
    for i, contact in enumerate(contacts):
        session.add(
            CampaignRecipient(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                contact_id=contact.id,
            )
        )
        session.add(
            DeliveryJob(
                tenant_id=tenant.id,
                campaign_id=campaign.id,
                recipient_id=contact.id,
                sender_id=sender.id,
                scheduled_at=datetime.now(UTC),
                status="DELIVERED" if i == 0 else "SENT",
                completed_at=datetime.now(UTC),
            )
        )
        message = Message(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            sender_id=sender.id,
            contact_id=contact.id,
            subject="Hello",
            status="DELIVERED",
        )
        session.add(message)
        session.flush()
        session.add(
            NormalizedDeliveryEvent(
                tenant_id=tenant.id,
                provider="GOOGLE",
                provider_event_id=f"e{contact.email}",
                message_id=message.id,
                recipient=contact.email,
                event_type="DELIVERED",
                event_time=datetime.now(UTC),
            )
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


def _auth(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_reports_dashboard(reports_client) -> None:
    client, token, _, _ = reports_client
    response = client.get("/api/v1/reports/dashboard", headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    assert body["contacts"]["total"] == 3
    assert body["contacts"]["unsubscribed"] == 1
    assert body["campaigns"]["total"] == 1
    assert body["campaigns"]["completed"] == 1
    assert body["delivery"]["sent"] == 3
    assert body["delivery"]["delivered"] == 3
    assert body["senders"]["connected"] == 1


def test_reports_contacts(reports_client) -> None:
    client, token, _, _ = reports_client
    response = client.get("/api/v1/reports/contacts", headers=_auth(token))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["active"] == 2
    assert body["unsubscribed"] == 1


def test_reports_campaigns(reports_client) -> None:
    client, token, _, campaign_id = reports_client
    response = client.get("/api/v1/reports/campaigns", headers=_auth(token))
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["campaign_id"] == str(campaign_id)
    assert rows[0]["recipients"] == 3
    assert rows[0]["delivered"] == 3


def test_reports_senders(reports_client) -> None:
    client, token, _, _ = reports_client
    response = client.get("/api/v1/reports/senders", headers=_auth(token))
    assert response.status_code == 200
    rows = response.json()
    assert len(rows) == 1
    assert rows[0]["sender_email"] == "sender@example.com"
    assert rows[0]["messages"] == 3
    assert rows[0]["successful"] == 3


def test_reports_dashboard_today_filter(reports_client) -> None:
    client, token, _, _ = reports_client
    response = client.get(
        "/api/v1/reports/dashboard?range=today", headers=_auth(token)
    )
    assert response.status_code == 200
    assert response.json()["delivery"]["sent"] == 3


def test_reports_dashboard_custom_filter(reports_client) -> None:
    client, token, _, _ = reports_client
    today = datetime.now(UTC).date().isoformat()
    response = client.get(
        f"/api/v1/reports/dashboard?range=custom&start_date={today}&end_date={today}",
        headers=_auth(token),
    )
    assert response.status_code == 200


def test_reports_bad_range_returns_422(reports_client) -> None:
    client, token, _, _ = reports_client
    response = client.get(
        "/api/v1/reports/dashboard?range=invalid", headers=_auth(token)
    )
    assert response.status_code == 422


def test_reports_export_csv(reports_client) -> None:
    client, token, _, _ = reports_client
    response = client.get(
        "/api/v1/reports/export?report=campaigns", headers=_auth(token)
    )
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("text/csv")
    assert "Content-Disposition" in response.headers
    csv_lines = response.text.strip().splitlines()
    assert csv_lines[0].startswith("campaign_id,campaign_name")
    assert len(csv_lines) == 2


def test_reports_export_invalid_report_422(reports_client) -> None:
    client, token, _, _ = reports_client
    response = client.get(
        "/api/v1/reports/export?report=unknown", headers=_auth(token)
    )
    assert response.status_code == 422


def test_reports_requires_authentication(reports_client) -> None:
    client, _token, _, _ = reports_client
    response = client.get("/api/v1/reports/dashboard")
    assert response.status_code in (401, 403)