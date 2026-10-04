from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    Campaign,
    Contact,
    EmailAccount,
    Message,
    Suppression,
    Tenant,
)
from app.services.compliance import ComplianceService
from app.services.suppression import SuppressionError, SuppressionService


@pytest.fixture()
def suppression_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'suppression.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Suppression Tenant", slug=f"suppression-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        contact = Contact(tenant_id=tenant.id, email="recipient@example.com", validation_status="VALID")
        sender = EmailAccount(tenant_id=tenant.id, provider="SMTP", email="sender@example.com", status="CONNECTED")
        session.add_all([contact, sender])
        session.flush()
        campaign = Campaign(tenant_id=tenant.id, name="Campaign", objective="Test", sender_id=sender.id, status="APPROVED", schedule_config={})
        session.add(campaign)
        session.flush()
        message = Message(tenant_id=tenant.id, campaign_id=campaign.id, sender_id=sender.id, contact_id=contact.id, subject="Test", status="SENT")
        session.add(message)
        session.commit()
        yield session, tenant.id, contact, sender, campaign, message
    engine.dispose()


def test_unsubscribe_suppresses_across_campaigns(suppression_fixture) -> None:
    session, tenant_id, contact, sender, campaign, _message = suppression_fixture
    service = SuppressionService(session, tenant_id)
    token = service.create_unsubscribe_token(contact.email, contact.id, campaign.id)
    service.consume_unsubscribe_token(token)
    assert service.is_suppressed(contact.email)
    assert session.query(Suppression).one().reason == "UNSUBSCRIBED"
    result = ComplianceService(session, tenant_id).evaluate(campaign, sender, contact)
    assert any(check.name == "suppression" for check in result.failures)
    with pytest.raises(SuppressionError):
        service.consume_unsubscribe_token("invalid-token")


def test_hard_bounce_suppresses_recipient(suppression_fixture) -> None:
    session, tenant_id, contact, sender, campaign, message = suppression_fixture
    assert SuppressionService(session, tenant_id).process_bounce(message.id, "HARD_BOUNCE") == "SUPPRESSED"
    assert session.query(Suppression).one().reason == "HARD_BOUNCE"
    assert ComplianceService(session, tenant_id).evaluate(campaign, sender, contact).outcome == "BLOCK"


def test_temporary_failure_retries_then_quarantines(suppression_fixture) -> None:
    session, tenant_id, _contact, _sender, _campaign, message = suppression_fixture
    service = SuppressionService(session, tenant_id)
    assert service.process_bounce(message.id, "TEMPORARY_FAILURE") == "RETRY"
    assert service.process_bounce(message.id, "TEMPORARY_FAILURE") == "RETRY"
    assert service.process_bounce(message.id, "TEMPORARY_FAILURE") == "SUPPRESSED"
    assert session.query(Suppression).one().reason == "POLICY_BLOCK"


def test_manual_block_is_tenant_scoped(suppression_fixture) -> None:
    session, tenant_id, contact, sender, campaign, _message = suppression_fixture
    record = SuppressionService(session, tenant_id).suppress(contact.email, "MANUAL_BLOCK", "manual", contact.id)
    assert record.reason == "MANUAL_BLOCK"
    assert ComplianceService(session, tenant_id).evaluate(campaign, sender, contact).outcome == "BLOCK"
    assert not SuppressionService(session, uuid4()).is_suppressed(contact.email)
