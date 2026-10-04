from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    Campaign,
    CampaignRecipient,
    Contact,
    EmailAccount,
    ScheduledMessage,
    Suppression,
    Template,
    TemplateVersion,
    Tenant,
    Unsubscribe,
)
from app.providers import MockEmailProvider, ProviderProfile
from app.services.compliance import ComplianceService
from app.services.sending import SendBlockedError, SendingService


@pytest.fixture()
def send_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'sending.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Send Tenant", slug=f"send-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        sender = EmailAccount(
            tenant_id=tenant.id,
            provider="SMTP",
            email="sender@example.com",
            status="CONNECTED",
        )
        contact = Contact(
            tenant_id=tenant.id,
            email="recipient@example.com",
            first_name="Recipient",
            validation_status="VALID",
        )
        template = Template(tenant_id=tenant.id, name="Send template")
        session.add_all([sender, contact, template])
        session.flush()
        version = TemplateVersion(
            tenant_id=tenant.id,
            template_id=template.id,
            version_number=1,
            subject_template="Hello {{first_name}}",
            html_body="<p>Hello {{first_name}}</p>",
            variable_manifest=["first_name"],
            status="ACTIVE",
        )
        session.add(version)
        session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            name="Send campaign",
            objective="Test",
            sender_id=sender.id,
            template_version_id=version.id,
            status="APPROVED",
            schedule_config={},
        )
        session.add(campaign)
        session.flush()
        recipient = CampaignRecipient(
            tenant_id=tenant.id, campaign_id=campaign.id, contact_id=contact.id
        )
        session.add(recipient)
        session.flush()
        scheduled = ScheduledMessage(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            campaign_recipient_id=recipient.id,
            due_at=datetime.now(UTC),
            idempotency_key=f"send:{recipient.id}",
        )
        session.add(scheduled)
        session.commit()
        yield session, tenant.id, campaign, sender, contact, scheduled
    engine.dispose()


def test_suppressed_recipient_is_blocked_and_audited(send_fixture) -> None:
    session, tenant_id, _campaign, sender, contact, scheduled = send_fixture
    session.add(
        Suppression(
            tenant_id=tenant_id,
            email=contact.email,
            reason="MANUAL_BLOCK",
            source="test",
            effective_at=datetime.now(UTC),
        )
    )
    session.commit()
    with pytest.raises(SendBlockedError) as error:
        SendingService(
            session,
            tenant_id,
            lambda _: MockEmailProvider("SMTP", ProviderProfile(sender.email)),
        ).send_scheduled(scheduled.id)
    assert any(check.name == "suppression" for check in error.value.result.failures)
    assert scheduled.status == "FAILED"


def test_unsubscribe_and_unapproved_campaign_are_blocked(send_fixture) -> None:
    session, tenant_id, campaign, sender, contact, _scheduled = send_fixture
    session.add(
        Unsubscribe(
            tenant_id=tenant_id,
            email=contact.email,
            token_hash=uuid4().hex,
            unsubscribed_at=datetime.now(UTC),
        )
    )
    campaign.status = "DRAFT"
    session.commit()
    result = ComplianceService(session, tenant_id).evaluate(campaign, sender, contact)
    assert {check.name for check in result.failures} >= {
        "unsubscribe",
        "campaign_approved",
    }


def test_scheduled_campaign_is_allowed_to_send(send_fixture) -> None:
    session, tenant_id, campaign, sender, contact, _scheduled = send_fixture
    campaign.status = "SCHEDULED"
    session.commit()

    result = ComplianceService(session, tenant_id).evaluate(campaign, sender, contact)

    assert not any(check.name == "campaign_approved" for check in result.failures)


def test_duplicate_worker_execution_sends_once(send_fixture) -> None:
    session, tenant_id, _campaign, sender, _contact, scheduled = send_fixture
    provider = MockEmailProvider("SMTP", ProviderProfile(sender.email))
    provider.connect()
    sending = SendingService(session, tenant_id, lambda _: provider)
    first = sending.send_scheduled(scheduled.id)
    second = sending.send_scheduled(scheduled.id)
    assert first.id == second.id
    assert len(provider.sent) == 1
    assert session.query(ScheduledMessage).one().status == "SENT"


def test_sender_and_domain_policy_checks(send_fixture) -> None:
    session, tenant_id, campaign, sender, contact, _scheduled = send_fixture
    sender.status = "DISABLED"
    campaign.schedule_config = {
        "tenant_policy": {"sending_enabled": False},
        "unsubscribe_required": True,
    }
    session.commit()
    result = ComplianceService(session, tenant_id).evaluate(campaign, sender, contact)
    assert {check.name for check in result.failures} >= {
        "sender_connected",
        "sender_health",
        "provider_capacity",
        "unsubscribe_mechanism",
        "tenant_policy",
    }
