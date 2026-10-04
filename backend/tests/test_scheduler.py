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
    Tenant,
)
from app.services.scheduler import (
    SchedulerError,
    SchedulerService,
    SchedulingNotAllowedError,
)
from app.tasks import scheduler as scheduler_tasks
from app.workers.scheduler import ScheduledMessageWorker


@pytest.fixture()
def scheduler_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'scheduler.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Schedule Tenant", slug=f"schedule-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        sender = EmailAccount(tenant_id=tenant.id, provider="SMTP", email="sender@example.com", status="CONNECTED")
        contact = Contact(tenant_id=tenant.id, email="recipient@example.com", first_name="Recipient")
        session.add_all([sender, contact])
        session.flush()
        campaign = Campaign(tenant_id=tenant.id, name="Approved campaign", objective="Test", sender_id=sender.id, status="APPROVED", schedule_config={"start_at": datetime.now(UTC).isoformat(), "timezone_policy": "America/New_York", "max_attempts": 2})
        session.add(campaign)
        session.flush()
        recipient = CampaignRecipient(tenant_id=tenant.id, campaign_id=campaign.id, contact_id=contact.id)
        session.add(recipient)
        session.commit()
        yield session, tenant.id, campaign.id
    engine.dispose()


def test_only_approved_campaigns_schedule(scheduler_session) -> None:
    session, tenant_id, campaign_id = scheduler_session
    campaign = session.get(Campaign, campaign_id)
    campaign.status = "DRAFT"
    session.commit()
    with pytest.raises(SchedulingNotAllowedError):
        SchedulerService(session, tenant_id).schedule_campaign(campaign_id)


def test_schedule_preserves_requested_time_and_queue_state(scheduler_session) -> None:
    session, tenant_id, campaign_id = scheduler_session
    scheduled = SchedulerService(session, tenant_id).schedule_campaign(campaign_id)
    assert len(scheduled) == 1
    assert scheduled[0].status == "QUEUED"
    due_at = scheduled[0].due_at.replace(tzinfo=UTC) if scheduled[0].due_at.tzinfo is None else scheduled[0].due_at
    assert due_at <= datetime.now(UTC)


def test_naive_start_time_uses_campaign_timezone(scheduler_session) -> None:
    session, tenant_id, campaign_id = scheduler_session
    campaign = session.get(Campaign, campaign_id)
    campaign.schedule_config = {
        "start_at": "2026-08-25T10:00",
        "timezone_policy": "Asia/Kolkata",
    }
    session.commit()

    scheduled = SchedulerService(session, tenant_id).schedule_campaign(campaign_id)
    assert scheduled[0].due_at.hour == 4
    assert scheduled[0].due_at.minute == 30


def test_due_message_poller_enqueues_only_ready_messages(scheduler_session, monkeypatch) -> None:
    session, tenant_id, campaign_id = scheduler_session
    item = SchedulerService(session, tenant_id).schedule_campaign(campaign_id)[0]
    item.due_at = datetime.now(UTC)
    item.deferred_until = datetime.now(UTC).replace(year=2099)
    session.commit()

    class SessionContext:
        def __enter__(self):
            return session

        def __exit__(self, *_args):
            return False

    queued: list[tuple[str, str]] = []
    monkeypatch.setattr(scheduler_tasks, "SessionLocal", lambda: SessionContext())
    monkeypatch.setattr(
        scheduler_tasks.schedule_message,
        "delay",
        lambda tenant, scheduled: queued.append((tenant, scheduled)),
    )

    assert scheduler_tasks.dispatch_due_scheduled_messages.run() == 0
    assert queued == []

    item.deferred_until = None
    session.commit()
    assert scheduler_tasks.dispatch_due_scheduled_messages.run() == 1
    assert queued == [(str(tenant_id), str(item.id))]


def test_defer_backoff_and_dead_letter(scheduler_session) -> None:
    session, tenant_id, campaign_id = scheduler_session
    item = SchedulerService(session, tenant_id).schedule_campaign(campaign_id)[0]
    service = SchedulerService(session, tenant_id)
    first = service.defer(item.id, "throttled")
    assert first.status == "DEFERRED"
    assert first.attempt_count == 1
    assert first.deferred_until is not None
    second = service.defer(item.id, "throttled")
    assert second.status == "FAILED"
    assert second.failure_reason is not None and second.failure_reason.startswith("Retry limit")


def test_pause_resume_cancel_and_worker(scheduler_session) -> None:
    session, tenant_id, campaign_id = scheduler_session
    service = SchedulerService(session, tenant_id)
    item = service.schedule_campaign(campaign_id)[0]
    paused = service.pause_campaign(campaign_id)
    assert paused.status == "PAUSED"
    assert session.get(ScheduledMessage, item.id).status == "CANCELLED"
    assert service.resume_campaign(campaign_id).status == "SCHEDULED"
    assert session.get(ScheduledMessage, item.id).status == "QUEUED"
    assert service.cancel_campaign(campaign_id).status == "CANCELLED"

    campaign = session.get(Campaign, campaign_id)
    campaign.status = "APPROVED"
    session.commit()
    item = service.schedule_campaign(campaign_id)[0]
    class FakeSendingService:
        def send_scheduled(self, scheduled_id):
            return None

    worker = ScheduledMessageWorker(service, FakeSendingService())
    assert worker.process(item.id) == "SENT"
    assert session.get(ScheduledMessage, item.id).status == "SENT"


def test_throttling_worker_defers_without_rotation(scheduler_session) -> None:
    session, tenant_id, campaign_id = scheduler_session
    service = SchedulerService(session, tenant_id)
    item = service.schedule_campaign(campaign_id)[0]

    class ThrottledError(Exception):
        retry_after = 30

    class ThrottledSendingService:
        def send_scheduled(self, scheduled_id):
            raise ThrottledError()

    worker = ScheduledMessageWorker(service, ThrottledSendingService())
    assert worker.process(item.id) == "DEFERRED"
    assert session.get(ScheduledMessage, item.id).deferred_until is not None


def test_scheduler_is_tenant_scoped(scheduler_session) -> None:
    session, _, campaign_id = scheduler_session
    with pytest.raises((SchedulingNotAllowedError, SchedulerError, LookupError)):
        SchedulerService(session, uuid4()).schedule_campaign(campaign_id)
