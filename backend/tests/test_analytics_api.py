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
    Complaint,
    Contact,
    Domain,
    EmailAccount,
    Message,
    Permission,
    Reply,
    Role,
    RolePermission,
    Suppression,
    Tenant,
    Thread,
    User,
    UserRole,
)
from app.security.passwords import hash_password


@pytest.fixture()
def analytics_client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'analytics.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    tenant_a = Tenant(name="Alpha", slug=f"alpha-{uuid4().hex[:8]}")
    tenant_b = Tenant(name="Beta", slug=f"beta-{uuid4().hex[:8]}")
    session.add_all([tenant_a, tenant_b])
    session.flush()

    user_a = User(
        tenant_id=tenant_a.id,
        email="owner@example.com",
        password_hash=hash_password("correct horse battery staple"),
        display_name="Owner",
    )
    role_a = Role(tenant_id=tenant_a.id, name="Admin")
    permission = Permission(key="analytics.read", description="Read analytics")
    session.add_all([user_a, role_a, permission])
    session.flush()
    session.add_all([
        UserRole(tenant_id=tenant_a.id, user_id=user_a.id, role_id=role_a.id),
        RolePermission(role_id=role_a.id, permission_id=permission.id),
    ])

    sender_a = EmailAccount(tenant_id=tenant_a.id, provider="SMTP", email="hello@alpha.example.com", display_name="Alpha Sender")
    sender_b = EmailAccount(tenant_id=tenant_b.id, provider="SMTP", email="hello@beta.example.com", display_name="Beta Sender")
    contact_a = Contact(tenant_id=tenant_a.id, email="prospect@alpha.example.com")
    contact_b = Contact(tenant_id=tenant_b.id, email="prospect@beta.example.com")
    domain_a = Domain(tenant_id=tenant_a.id, domain="alpha.example.com", health_status="HEALTHY")
    domain_b = Domain(tenant_id=tenant_b.id, domain="beta.example.com", health_status="WARN")
    session.add_all([sender_a, sender_b, contact_a, contact_b, domain_a, domain_b])
    session.flush()

    campaign_a = Campaign(
        tenant_id=tenant_a.id,
        name="Alpha launch",
        objective="Launch campaign",
        sender_id=sender_a.id,
        status="RUNNING",
        schedule_config={},
        created_by_id=user_a.id,
    )
    campaign_b = Campaign(
        tenant_id=tenant_b.id,
        name="Beta launch",
        objective="Other tenant",
        sender_id=sender_b.id,
        status="RUNNING",
        schedule_config={},
        created_by_id=user_a.id,
    )
    session.add_all([campaign_a, campaign_b])
    session.flush()

    thread_a = Thread(
        tenant_id=tenant_a.id,
        provider_thread_id="thread-a",
        sender_id=sender_a.id,
        contact_id=contact_a.id,
        campaign_id=campaign_a.id,
        status="OPEN",
        subject="Welcome",
        assigned_user_id=user_a.id,
    )
    thread_b = Thread(
        tenant_id=tenant_b.id,
        provider_thread_id="thread-b",
        sender_id=sender_b.id,
        contact_id=contact_b.id,
        campaign_id=campaign_b.id,
        status="OPEN",
        subject="Other",
        assigned_user_id=user_a.id,
    )
    session.add_all([thread_a, thread_b])
    session.flush()

    session.add_all([
        Message(tenant_id=tenant_a.id, campaign_id=campaign_a.id, sender_id=sender_a.id, contact_id=contact_a.id, subject="Alpha subject", status="QUEUED"),
        Message(tenant_id=tenant_a.id, campaign_id=campaign_a.id, sender_id=sender_a.id, contact_id=contact_a.id, subject="Alpha subject", status="SENT"),
        Message(tenant_id=tenant_a.id, campaign_id=campaign_a.id, sender_id=sender_a.id, contact_id=contact_a.id, subject="Alpha subject", status="ACCEPTED"),
        Message(tenant_id=tenant_a.id, campaign_id=campaign_a.id, sender_id=sender_a.id, contact_id=contact_a.id, subject="Alpha subject", status="FAILED"),
        Message(tenant_id=tenant_b.id, campaign_id=campaign_b.id, sender_id=sender_b.id, contact_id=contact_b.id, subject="Beta subject", status="SENT"),
    ])
    session.flush()

    session.add_all([
        Reply(tenant_id=tenant_a.id, thread_id=thread_a.id, sender_email="prospect@alpha.example.com", recipient_email="hello@alpha.example.com", body_text="Interested in learning more", classification="INTERESTED", suggested_action="SCHEDULE_FOLLOW_UP", approval_status="APPROVED", received_at=datetime.now(UTC)),
        Reply(tenant_id=tenant_a.id, thread_id=thread_a.id, sender_email="prospect@alpha.example.com", recipient_email="hello@alpha.example.com", body_text="Please unsubscribe me", classification="UNSUBSCRIBE", suggested_action="SUPPRESS_CONTACT", approval_status="APPROVED", received_at=datetime.now(UTC)),
        Reply(tenant_id=tenant_b.id, thread_id=thread_b.id, sender_email="prospect@beta.example.com", recipient_email="hello@beta.example.com", body_text="Interested", classification="INTERESTED", suggested_action="SCHEDULE_FOLLOW_UP", approval_status="APPROVED", received_at=datetime.now(UTC)),
    ])

    session.add_all([
        Suppression(tenant_id=tenant_a.id, email="prospect@alpha.example.com", reason="UNSUBSCRIBED", source="reply-unsubscribe", effective_at=datetime.now(UTC)),
        Complaint(tenant_id=tenant_a.id, message_id=next(m.id for m in session.query(Message).filter_by(tenant_id=tenant_a.id, status="SENT").all()[:1]), provider_reference="complaint-1", occurred_at=datetime.now(UTC)),
    ])

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
    yield client, tenant_a.id
    app.dependency_overrides.clear()
    engine.dispose()


def test_analytics_dashboard_metrics_are_tenant_isolated(analytics_client) -> None:
    client, tenant_id = analytics_client
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "correct horse battery staple"},
    )
    assert response.status_code == 200
    token = response.json()["access_token"]

    dashboard = client.get("/api/v1/analytics/dashboard", headers={"Authorization": f"Bearer {token}"})
    assert dashboard.status_code == 200

    payload = dashboard.json()
    assert payload["tenant_id"] == str(tenant_id)
    assert payload["metrics"]["campaigns"] == 1
    assert payload["metrics"]["recipients"] >= 1
    assert payload["metrics"]["queued"] == 1
    assert payload["metrics"]["sent"] == 2
    assert payload["metrics"]["provider_accepted"] == 1
    assert payload["metrics"]["bounced"] == 0
    assert payload["metrics"]["failed"] == 1
    assert payload["metrics"]["replies"] == 2
    assert payload["metrics"]["positive_replies"] == 1
    assert payload["metrics"]["unsubscribes"] == 1
    assert payload["metrics"]["complaints"] == 1


def test_analytics_reports_are_available(analytics_client) -> None:
    client, _ = analytics_client
    response = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "correct horse battery staple"},
    )
    token = response.json()["access_token"]

    for path in ("/api/v1/analytics/campaigns", "/api/v1/analytics/senders", "/api/v1/analytics/domains"):
        api_response = client.get(path, headers={"Authorization": f"Bearer {token}"})
        assert api_response.status_code == 200
        assert isinstance(api_response.json(), list)
