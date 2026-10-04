from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Base,
    Bounce,
    Campaign,
    Complaint,
    Contact,
    Domain,
    DomainCheck,
    EmailAccount,
    Message,
    MessageEvent,
    SenderHealthHistory,
    Tenant,
)
from app.services.health import DomainHealthService, SenderHealthService


@pytest.fixture()
def dh_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'dh.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="DH Tenant", slug=f"dh-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        yield session, tenant.id
    engine.dispose()


def _sender(session: Session, tenant_id, *, email: str | None = None, status: str = "CONNECTED") -> EmailAccount:
    account = EmailAccount(
        tenant_id=tenant_id,
        provider="SMTP",
        email=email or f"sender-{uuid4().hex[:6]}@example.com",
        display_name="Sender",
        status=status,
        health_score=100,
    )
    session.add(account)
    session.flush()
    return account


def _message(session: Session, tenant_id, sender, contact, status: str = "SENT") -> Message:
    msg = Message(
        tenant_id=tenant_id,
        campaign_id=None,
        sender_id=sender.id,
        contact_id=contact.id,
        subject="Health test",
        status=status,
    )
    session.add(msg)
    session.flush()
    return msg


def test_provider_error_lowers_score_and_returns_failure_reason(dh_session) -> None:
    session, tenant_id = dh_session
    sender = _sender(session, tenant_id)
    contact = Contact(tenant_id=tenant_id, email="user@example.com")
    session.add(contact)
    session.flush()
    _message(session, tenant_id, sender, contact, status="FAILED")
    _message(session, tenant_id, sender, contact, status="FAILED")
    session.commit()

    result = SenderHealthService(session, tenant_id).evaluate(sender.id)

    assert result["factors"]["provider_errors"] == 2
    assert result["health_score"] < 100
    assert any("provider error" in reason for reason in result["failure_reasons"])
    assert result["state"] in {"HEALTHY", "WARNING", "CRITICAL"}


def test_auth_failure_lowers_score(dh_session) -> None:
    session, tenant_id = dh_session
    sender = _sender(session, tenant_id)
    contact = Contact(tenant_id=tenant_id, email="user@example.com")
    session.add(contact)
    session.flush()
    msg = _message(session, tenant_id, sender, contact)
    session.add(
        MessageEvent(tenant_id=tenant_id, message_id=msg.id, event_type="AUTH_ERROR", provider_payload={}, occurred_at=datetime.now(UTC))
    )
    session.commit()

    result = SenderHealthService(session, tenant_id).evaluate(sender.id)

    assert result["factors"]["authentication_failures"] == 1
    assert result["factors"]["authentication"] is False
    assert result["health_score"] < 100


def test_bounce_increase_affects_health(dh_session) -> None:
    session, tenant_id = dh_session
    sender = _sender(session, tenant_id)
    contact = Contact(tenant_id=tenant_id, email="user@example.com")
    session.add(contact)
    session.flush()
    msg = _message(session, tenant_id, sender, contact)
    session.add(
        Bounce(tenant_id=tenant_id, message_id=msg.id, classification="HARD_BOUNCE", occurred_at=datetime.now(UTC))
    )
    session.commit()

    result = SenderHealthService(session, tenant_id).evaluate(sender.id)

    assert result["factors"]["hard_bounces"] == 1
    assert any("bounce" in reason for reason in result["failure_reasons"])


def test_complaint_event_affects_health(dh_session) -> None:
    session, tenant_id = dh_session
    sender = _sender(session, tenant_id)
    contact = Contact(tenant_id=tenant_id, email="user@example.com")
    session.add(contact)
    session.flush()
    msg = _message(session, tenant_id, sender, contact)
    session.add(Complaint(tenant_id=tenant_id, message_id=msg.id, occurred_at=datetime.now(UTC)))
    session.commit()

    result = SenderHealthService(session, tenant_id).evaluate(sender.id)

    assert result["factors"]["complaints"] == 1
    assert any("complaint" in reason for reason in result["failure_reasons"])


def test_disabled_sender_reports_disabled_state(dh_session) -> None:
    session, tenant_id = dh_session
    sender = _sender(session, tenant_id, status="DISABLED")
    session.commit()

    result = SenderHealthService(session, tenant_id).evaluate(sender.id)

    assert result["status"] == "DISABLED"
    assert result["state"] == "DISABLED"


def test_domain_dns_failure_marks_fail(dh_session) -> None:
    session, tenant_id = dh_session
    domain = Domain(tenant_id=tenant_id, domain="bad.example.com", health_status="UNKNOWN")
    session.add(domain)
    session.commit()

    class FailingResolver:
        def resolve_spf(self, domain):
            return False
        def resolve_dkim(self, domain, selector):
            return False
        def resolve_dmarc(self, domain):
            return False
        def resolve_mx(self, domain):
            return True

    result = DomainHealthService(session, tenant_id).evaluate(domain.id, resolver=FailingResolver())

    assert result["status"] == "FAIL"
    assert result["checks"]["SPF"]["status"] == "FAIL"
    assert "DNS" in result["disclaimer"]


def test_domain_unknown_when_dns_unavailable(dh_session) -> None:
    session, tenant_id = dh_session
    domain = Domain(tenant_id=tenant_id, domain="nores.example.com", health_status="UNKNOWN")
    session.add(domain)
    session.commit()

    class UnavailableResolver:
        def resolve_spf(self, domain):
            return None
        def resolve_dkim(self, domain, selector):
            return None
        def resolve_dmarc(self, domain):
            return None
        def resolve_mx(self, domain):
            return True

    result = DomainHealthService(session, tenant_id).evaluate(domain.id, resolver=UnavailableResolver())

    assert result["status"] == "UNKNOWN"
    assert result["checks"]["SPF"]["status"] == "UNKNOWN"


def test_domain_passes_with_good_dns(dh_session) -> None:
    session, tenant_id = dh_session
    domain = Domain(tenant_id=tenant_id, domain="good.example.com", health_status="UNKNOWN")
    session.add(domain)
    session.commit()

    class GoodResolver:
        def resolve_spf(self, domain):
            return True
        def resolve_dkim(self, domain, selector):
            return True
        def resolve_dmarc(self, domain):
            return True
        def resolve_mx(self, domain):
            return True

    result = DomainHealthService(session, tenant_id).evaluate(domain.id, resolver=GoodResolver())

    assert result["status"] == "PASS"


def test_domain_checks_persisted(dh_session) -> None:
    session, tenant_id = dh_session
    domain = Domain(tenant_id=tenant_id, domain="persist.example.com", health_status="UNKNOWN")
    session.add(domain)
    session.commit()
    DomainHealthService(session, tenant_id).evaluate(domain.id)

    rows = session.scalars(
        select(DomainCheck).where(DomainCheck.tenant_id == tenant_id, DomainCheck.domain_id == domain.id)
    ).all()

    assert {row.check_type for row in rows} == {"SPF", "DKIM", "DMARC", "MX"}


def test_sender_history_is_recorded(dh_session) -> None:
    session, tenant_id = dh_session
    sender = _sender(session, tenant_id)
    session.commit()
    SenderHealthService(session, tenant_id).evaluate(sender.id)

    rows = session.scalars(
        select(SenderHealthHistory).where(SenderHealthHistory.tenant_id == tenant_id, SenderHealthHistory.sender_id == sender.id)
    ).all()

    assert len(rows) == 1
    assert rows[0].status in {"HEALTHY", "HEALTH_WARNING", "HEALTH_CRITICAL"}


def test_critical_sender_pauses_approved_campaign(dh_session) -> None:
    session, tenant_id = dh_session
    sender = _sender(session, tenant_id, status="HEALTH_WARNING")
    campaign = Campaign(
        tenant_id=tenant_id,
        name="Blocked by critical",
        objective="Launch",
        sender_id=sender.id,
        status="APPROVED",
        schedule_config={},
    )
    session.add(campaign)
    session.commit()

    SenderHealthService(session, tenant_id).evaluate(sender.id)

    assert session.get(Campaign, campaign.id).status == "PAUSED"
    assert session.scalar(
        select(AuditLog).where(
            AuditLog.tenant_id == tenant_id,
            AuditLog.action == "campaign_paused_sender_critical",
        )
    ) is not None


def test_tenant_isolation_in_health_factors(dh_session) -> None:
    session, tenant_id = dh_session
    other = Tenant(name="Other Tenant", slug=f"other-{uuid4().hex[:8]}")
    session.add(other)
    session.flush()
    sender_a = _sender(session, tenant_id)
    sender_b = _sender(session, other.id)
    contact_b = Contact(tenant_id=other.id, email="b@example.com")
    session.add(contact_b)
    session.flush()
    _message(session, other.id, sender_b, contact_b, status="FAILED")
    session.commit()

    result_a = SenderHealthService(session, tenant_id).evaluate(sender_a.id)

    assert result_a["factors"]["provider_errors"] == 0
