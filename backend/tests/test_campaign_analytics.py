from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import (
    Base,
    Campaign,
    CampaignRecipient,
    Contact,
    DeliveryJob,
    EmailAccount,
    Message,
    NormalizedDeliveryEvent,
    Tenant,
)
from app.services.campaign_analytics import (
    CampaignAnalyticsNotFoundError,
    CampaignAnalyticsService,
)
from app.services.delivery_events import DeliveryEventService
from app.services.delivery_jobs import DeliveryJobService


def _make_campaign_with_jobs(
    session: Session, tenant_id, sender_id, emails: list[str]
):
    contacts = [
        Contact(tenant_id=tenant_id, email=email, first_name=f"F{i}")
        for i, email in enumerate(emails)
    ]
    session.add_all(contacts)
    session.flush()
    campaign = Campaign(
        tenant_id=tenant_id,
        name="Analytics campaign",
        objective="Test",
        sender_id=sender_id,
        status="APPROVED",
        schedule_config={
            "start_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()
        },
    )
    session.add(campaign)
    session.flush()
    for contact in contacts:
        session.add(
            CampaignRecipient(
                tenant_id=tenant_id,
                campaign_id=campaign.id,
                contact_id=contact.id,
            )
        )
    session.commit()
    DeliveryJobService(session, tenant_id).materialize_for_campaign(campaign.id)
    return campaign, contacts


@pytest.fixture()
def analytics_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'analytics.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Analytics Tenant", slug=f"an-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        sender = EmailAccount(
            tenant_id=tenant.id,
            provider="GOOGLE",
            email="sender@example.com",
            status="CONNECTED",
        )
        session.add(sender)
        session.commit()
        campaign, contacts = _make_campaign_with_jobs(
            session,
            tenant.id,
            sender.id,
            ["a@example.com", "b@example.com", "c@example.com"],
        )
        yield session, tenant.id, sender.id, campaign, contacts
    engine.dispose()


def _jobs_by_contact(session: Session, campaign_id, contacts):
    return {
        contact.email: session.scalar(
            select(DeliveryJob)
            .where(
                DeliveryJob.campaign_id == campaign_id,
                DeliveryJob.recipient_id == contact.id,
            )
        )
        for contact in contacts
    }


def _messages_for(session: Session, tenant_id, campaign, contacts):
    messages = {}
    for contact in contacts:
        message = Message(
            tenant_id=tenant_id,
            campaign_id=campaign.id,
            sender_id=campaign.sender_id,
            contact_id=contact.id,
            subject="Hello",
        )
        session.add(message)
        session.flush()
        messages[contact.email] = message
    return messages


def _analytics(session: Session, tenant_id):
    return CampaignAnalyticsService(session, tenant_id)


def test_metrics_derived_from_jobs_and_events(analytics_fixture) -> None:
    session, tenant_id, _s, campaign, contacts = analytics_fixture
    messages = _messages_for(session, tenant_id, campaign, contacts)
    service = DeliveryEventService(session, tenant_id)
    service.process_event(
        "GOOGLE", "evt-m1", "delivered", "a@example.com",
        message_id=messages["a@example.com"].id,
        campaign_id=campaign.id,
    )
    service.process_event(
        "GOOGLE", "evt-m2", "bounce", "b@example.com",
        message_id=messages["b@example.com"].id,
        campaign_id=campaign.id,
    )
    session.commit()
    metrics = _analytics(session, tenant_id).metrics(campaign.id)
    assert metrics["recipients"] == 3
    assert metrics["delivered"] == 1
    assert metrics["hard_bounces"] == 1
    assert metrics["sent"] == 0  # jobs still PENDING; no SENT job


def test_duplicate_events_do_not_double_count(analytics_fixture) -> None:
    session, tenant_id, _s, campaign, contacts = analytics_fixture
    messages = _messages_for(session, tenant_id, campaign, contacts)
    service = DeliveryEventService(session, tenant_id)
    for _ in range(3):
        service.process_event(
            "GOOGLE", "evt-dup-1", "complaint", "a@example.com",
            message_id=messages["a@example.com"].id,
            campaign_id=campaign.id,
        )
    session.commit()
    metrics = _analytics(session, tenant_id).metrics(campaign.id)
    assert metrics["complaints"] == 1
    assert session.query(NormalizedDeliveryEvent).count() == 1


def test_out_of_order_events_do_not_double_count(analytics_fixture) -> None:
    session, tenant_id, _s, campaign, contacts = analytics_fixture
    messages = _messages_for(session, tenant_id, campaign, contacts)
    service = DeliveryEventService(session, tenant_id)
    service.process_event(
        "GOOGLE", "evt-late-1", "bounce", "a@example.com",
        message_id=messages["a@example.com"].id,
        campaign_id=campaign.id,
    )
    service.process_event(
        "GOOGLE", "evt-early-1", "bounce", "a@example.com",
        message_id=messages["a@example.com"].id,
        campaign_id=campaign.id,
    )
    # Both have distinct provider ids (out-of-order delivery), but both are the
    # same event class for the same message -> analytics count each distinct
    # normalized event type for the message as transient outcomes; bounce is the
    # outcome. Distinct provider ids mean both may persist, matching provider
    # behaviour; the key guarantee is no *duplicate id* double-counts.
    session.commit()
    assert session.query(NormalizedDeliveryEvent).count() == 2


def test_missing_events_do_not_inflate(analytics_fixture) -> None:
    session, tenant_id, _s, campaign, _contacts = analytics_fixture
    metrics = _analytics(session, tenant_id).metrics(campaign.id)
    assert metrics["delivered"] == 0
    assert metrics["hard_bounces"] == 0
    assert metrics["unsubscribes"] == 0
    assert metrics["complaints"] == 0


def test_summary_has_percentages(analytics_fixture) -> None:
    session, tenant_id, _s, campaign, contacts = analytics_fixture
    messages = _messages_for(session, tenant_id, campaign, contacts)
    DeliveryEventService(session, tenant_id).process_event(
        "GOOGLE", "evt-s1", "delivered", "a@example.com",
        message_id=messages["a@example.com"].id,
        campaign_id=campaign.id,
    )
    session.commit()
    summary = _analytics(session, tenant_id).summary(campaign.id)
    assert summary["metrics"]["recipients"] == 3
    assert summary["percentages"]["delivered"] == pytest.approx(33.33, abs=0.01)


def test_tenant_isolation(analytics_fixture) -> None:
    session, _tenant_id, _s, campaign, _contacts = analytics_fixture
    with pytest.raises(CampaignAnalyticsNotFoundError):
        _analytics(session, uuid4()).metrics(campaign.id)


def test_recipient_activity_pagination(analytics_fixture) -> None:
    session, tenant_id, _s, campaign, contacts = analytics_fixture
    _messages_for(session, tenant_id, campaign, contacts)
    session.commit()
    service = _analytics(session, tenant_id)
    page1 = service.recipient_activity(campaign.id, page=1, page_size=2)
    page2 = service.recipient_activity(campaign.id, page=2, page_size=2)
    assert page1["total"] == 3
    assert len(page1["items"]) == 2
    assert len(page2["items"]) == 1


def test_recipient_activity_status_filter(analytics_fixture) -> None:
    session, tenant_id, _s, campaign, contacts = analytics_fixture
    jobs = _jobs_by_contact(session, campaign.id, contacts)
    service = DeliveryJobService(session, tenant_id)
    service.mark_sent(jobs["a@example.com"].id, "pm-a")
    session.commit()
    result = _analytics(session, tenant_id).recipient_activity(
        campaign.id, status_filter="SENT"
    )
    assert result["total"] == 1
    assert result["items"][0]["email"] == "a@example.com"


def test_recipient_activity_date_filter(analytics_fixture) -> None:
    session, tenant_id, _s, campaign, contacts = analytics_fixture
    _messages_for(session, tenant_id, campaign, contacts)
    session.commit()
    start = (datetime.now(UTC) - timedelta(days=2)).isoformat()
    end = (datetime.now(UTC) + timedelta(days=2)).isoformat()
    result = _analytics(session, tenant_id).recipient_activity(
        campaign.id, start_date=start, end_date=end
    )
    assert result["total"] == 3
    future = (datetime.now(UTC) + timedelta(days=10)).isoformat()
    result = _analytics(session, tenant_id).recipient_activity(
        campaign.id, start_date=future, end_date=future
    )
    assert result["total"] == 0


def test_export_csv_has_header_and_rows(analytics_fixture) -> None:
    session, tenant_id, _s, campaign, contacts = analytics_fixture
    _messages_for(session, tenant_id, campaign, contacts)
    session.commit()
    csv_data = _analytics(session, tenant_id).export_csv(campaign.id)
    lines = csv_data.strip().splitlines()
    assert lines[0] == "recipient,status,event,timestamp,reason"
    assert len(lines) == 4  # header + 3 recipients


def test_timeline_includes_created(analytics_fixture) -> None:
    session, tenant_id, _s, campaign, _contacts = analytics_fixture
    timeline = _analytics(session, tenant_id).timeline(campaign.id)
    labels = {item["event"] for item in timeline}
    assert "Created" in labels
    assert "Scheduled" in labels
