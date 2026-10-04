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
    NormalizedDeliveryEvent,
    SuppressionEntry,
    Tenant,
)
from app.services.delivery_events import DeliveryEventService
from app.services.suppression_engine import SuppressionEngine


@pytest.fixture()
def delivery_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'delivery.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Delivery Tenant", slug=f"deliv-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        sender = EmailAccount(tenant_id=tenant.id, provider="GOOGLE", email="sender@example.com", status="CONNECTED")
        contact = Contact(tenant_id=tenant.id, email="recipient@example.com", validation_status="VALID")
        session.add_all([sender, contact])
        session.flush()
        campaign = Campaign(tenant_id=tenant.id, name="Delivery campaign", objective="Test", sender_id=sender.id, status="APPROVED", schedule_config={})
        session.add(campaign)
        session.flush()
        message = Message(tenant_id=tenant.id, campaign_id=campaign.id, sender_id=sender.id, contact_id=contact.id, subject="Hello")
        session.add(message)
        session.commit()
        yield session, tenant.id, contact, campaign, message
    engine.dispose()


def test_hard_bounce_suppresses_and_is_idempotent(delivery_fixture) -> None:
    session, tenant_id, contact, _campaign, message = delivery_fixture
    service = DeliveryEventService(session, tenant_id)
    first = service.process_event(
        "GOOGLE", "evt-hard-1", "bounce", contact.email,
        message_id=message.id, campaign_id=message.campaign_id, raw_reference="550 5.1.1",
    )
    assert first.event_type == "HARD_BOUNCE"
    assert SuppressionEngine(session, tenant_id).is_suppressed(contact.email)
    entry = session.query(SuppressionEntry).one()
    assert entry.type == "HARD_BOUNCE"
    session.refresh(message)
    assert message.status == "BOUNCED"
    # Duplicate delivery of the same provider event id must not re-apply.
    second = service.process_event(
        "GOOGLE", "evt-hard-1", "bounce", contact.email,
        message_id=message.id, campaign_id=message.campaign_id, raw_reference="550 5.1.1",
    )
    assert second.id == first.id
    assert session.query(NormalizedDeliveryEvent).count() == 1
    assert session.query(SuppressionEntry).count() == 1


def test_complaint_suppresses_recipient(delivery_fixture) -> None:
    session, tenant_id, contact, _campaign, message = delivery_fixture
    DeliveryEventService(session, tenant_id).process_event(
        "MICROSOFT", "evt-comp-1", "complaint", contact.email,
        message_id=message.id, campaign_id=message.campaign_id,
    )
    session.refresh(message)
    assert message.status == "COMPLAINED"
    assert SuppressionEngine(session, tenant_id).is_suppressed(contact.email)
    assert session.query(SuppressionEntry).one().type == "COMPLAINT"


def test_unsubscribe_suppresses_recipient(delivery_fixture) -> None:
    session, tenant_id, contact, _campaign, message = delivery_fixture
    DeliveryEventService(session, tenant_id).process_event(
        "SMTP", "evt-unsub-1", "unsubscribe", contact.email,
        message_id=message.id, campaign_id=message.campaign_id,
    )
    assert SuppressionEngine(session, tenant_id).is_suppressed(contact.email)
    assert session.query(SuppressionEntry).one().type == "UNSUBSCRIBED"


def test_delivered_updates_message(delivery_fixture) -> None:
    session, tenant_id, contact, _campaign, message = delivery_fixture
    DeliveryEventService(session, tenant_id).process_event(
        "GOOGLE", "evt-deliv-1", "delivered", contact.email,
        message_id=message.id, campaign_id=message.campaign_id,
    )
    session.refresh(message)
    assert message.status == "DELIVERED"
    assert not SuppressionEngine(session, tenant_id).is_suppressed(contact.email)


def test_repeated_temporary_failure_threshold_suppresses(delivery_fixture) -> None:
    session, tenant_id, contact, _campaign, message = delivery_fixture
    service = DeliveryEventService(session, tenant_id)
    for index in range(1, 4):
        service.process_event(
            "GOOGLE", f"evt-temp-{index}", "deferred", contact.email,
            message_id=message.id, campaign_id=message.campaign_id,
            repeated_failure_threshold=3,
        )
    assert SuppressionEngine(session, tenant_id).is_suppressed(contact.email)
    assert session.query(SuppressionEntry).one().type == "MANUAL"


def test_unknown_event_is_stored_not_suppressing(delivery_fixture) -> None:
    session, tenant_id, contact, _campaign, message = delivery_fixture
    record = DeliveryEventService(session, tenant_id).process_event(
        "SMTP", "evt-unk-1", "message_deleted", contact.email,
        message_id=message.id, campaign_id=message.campaign_id,
    )
    assert record.event_type == "UNKNOWN"
    assert not SuppressionEngine(session, tenant_id).is_suppressed(contact.email)


def test_cross_tenant_isolation(delivery_fixture) -> None:
    session, tenant_id, contact, _campaign, message = delivery_fixture
    session.expire_all()
    other = Tenant(name="Other", slug=f"other-{uuid4().hex[:8]}")
    session.add(other)
    session.flush()
    DeliveryEventService(session, tenant_id).process_event(
        "GOOGLE", "evt-bounce-x", "bounce", contact.email,
        message_id=message.id, campaign_id=message.campaign_id,
    )
    session.commit()
    session.expire_all()
    assert SuppressionEngine(session, tenant_id).is_suppressed(contact.email)
    assert not SuppressionEngine(session, other.id).is_suppressed(contact.email)


def test_recipient_email_is_normalized(delivery_fixture) -> None:
    session, tenant_id, _contact, _campaign, _message = delivery_fixture
    DeliveryEventService(session, tenant_id).process_event(
        "SMTP", "evt-norm-1", "complainT", " Foo@Example.COM ",
    )
    record = session.query(NormalizedDeliveryEvent).one()
    assert record.recipient == "foo@example.com"
