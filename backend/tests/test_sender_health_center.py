from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import AuditLog, Base, Campaign, Contact, Domain, EmailAccount, Tenant
from app.services.health import DomainHealthService, SenderHealthService


@pytest.fixture()
def health_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'health.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Health Tenant", slug=f"health-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        yield session, tenant.id
    engine.dispose()


def test_sender_health_service_marks_critical_and_pauses_campaigns(health_session) -> None:
    session, tenant_id = health_session
    sender = EmailAccount(
        tenant_id=tenant_id,
        provider="SMTP",
        email="sender@example.com",
        display_name="Sender",
        status="HEALTH_WARNING",
        health_score=60,
    )
    contact = Contact(tenant_id=tenant_id, email="user@example.com")
    session.add_all([sender, contact])
    session.flush()
    campaign = Campaign(
        tenant_id=tenant_id,
        name="Critical sender campaign",
        objective="Launch",
        sender_id=sender.id,
        status="APPROVED",
        schedule_config={},
    )
    session.add(campaign)
    session.flush()

    session.add(
        EmailAccount(
            tenant_id=tenant_id,
            provider="SMTP",
            email="good@example.com",
            display_name="Other",
            status="CONNECTED",
            health_score=100,
        )
    )
    session.commit()

    result = SenderHealthService(session, tenant_id).evaluate(sender.id)

    assert result["status"] == "HEALTH_CRITICAL"
    assert result["factors"]["recent_failures"] >= 0
    assert session.get(Campaign, campaign.id).status == "PAUSED"
    assert session.scalar(
        select(AuditLog).where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == "campaign_paused_sender_critical",
        )
    ) is not None


def test_domain_health_service_creates_checks_and_remediation(health_session) -> None:
    session, tenant_id = health_session
    domain = Domain(tenant_id=tenant_id, domain="example.com", health_status="UNKNOWN")
    session.add(domain)
    session.commit()

    result = DomainHealthService(session, tenant_id).evaluate(domain.id)

    assert set(result["checks"]).issuperset({"SPF", "DKIM", "DMARC", "MX"})
    assert result["status"] in {"WARNING", "FAIL"}
    assert result["remediation"]
    for check in result["checks"].values():
        assert check["status"] in {"PASS", "WARNING", "FAIL"}
        assert "remediation" in check
