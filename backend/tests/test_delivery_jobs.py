from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    Campaign,
    CampaignRecipient,
    Contact,
    DeliveryJob,
    EmailAccount,
    SuppressionEntry,
    Tenant,
)
from app.providers import SenderUnavailableError
from app.services.delivery_jobs import (
    DeliveryJobError,
    DeliveryJobNotFoundError,
    DeliveryJobService,
)
from app.services.sending import SendBlockedError, SenderThrottledError, SendingService
from app.workers.delivery_jobs import DeliveryJobWorker


@pytest.fixture()
def dj_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'delivery.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Delivery Tenant", slug=f"delivery-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        sender = EmailAccount(tenant_id=tenant.id, provider="SMTP", email="sender@example.com", status="CONNECTED")
        contact = Contact(tenant_id=tenant.id, email="recipient@example.com", first_name="Recipient")
        session.add_all([sender, contact])
        session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            name="Delivery campaign",
            objective="Test",
            sender_id=sender.id,
            status="APPROVED",
            schedule_config={
                "start_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "max_attempts": 2,
            },
        )
        session.add(campaign)
        session.flush()
        recipient = CampaignRecipient(tenant_id=tenant.id, campaign_id=campaign.id, contact_id=contact.id)
        session.add(recipient)
        session.commit()
        yield session, tenant.id, campaign.id, sender.id, contact.id
    engine.dispose()


def _service(session: Session, tenant_id) -> DeliveryJobService:
    return DeliveryJobService(session, tenant_id)


def _naive(dt: datetime) -> datetime:
    return dt.replace(tzinfo=None) if dt.tzinfo is not None else dt


class _SentMessage:
    def __init__(self, provider_message_id: str = "pm-1") -> None:
        self.provider_message_id = provider_message_id


def _run_worker(session: Session, tenant_id, job_id, send):
    return DeliveryJobWorker(
        _service(session, tenant_id),
        send,
    ).process(job_id)


# ------------------------------------------------------------------ #
# Materialization + idempotency
# ------------------------------------------------------------------ #
def test_materialize_creates_one_job_per_recipient(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    jobs = _service(session, tenant_id).materialize_for_campaign(campaign_id)
    assert len(jobs) == 1
    assert jobs[0].status == "PENDING"
    assert session.get(Campaign, campaign_id).status == "SCHEDULED"


def test_materialize_is_idempotent_via_db_constraint(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    first = service.materialize_for_campaign(campaign_id)
    second = service.materialize_for_campaign(campaign_id)
    all_jobs = session.query(DeliveryJob).filter(DeliveryJob.campaign_id == campaign_id).all()
    assert len(all_jobs) == 1
    assert len(first) == 1 and len(second) == 1
    assert first[0].id == second[0].id


def test_materialize_rejects_draft_campaign(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    campaign = session.get(Campaign, campaign_id)
    campaign.status = "DRAFT"
    session.commit()
    with pytest.raises(DeliveryJobError):
        _service(session, tenant_id).materialize_for_campaign(campaign_id)


def test_suppressed_recipient_is_skipped_during_scheduling(dj_session) -> None:
    session, tenant_id, campaign_id, _, contact_id = dj_session
    contact = session.get(Contact, contact_id)
    session.add(
        SuppressionEntry(
            tenant_id=tenant_id,
            email_normalized=contact.email.lower(),
            type="MANUAL",
            source="test",
            active=True,
        )
    )
    session.commit()
    jobs = _service(session, tenant_id).materialize_for_campaign(campaign_id)
    assert jobs == []


# ------------------------------------------------------------------ #
# Concurrency-safe claiming
# ------------------------------------------------------------------ #
def test_claim_once_only(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]
    worker_a = uuid4()
    worker_b = uuid4()
    claimed = service.claim(job.id, worker_a)
    assert claimed is not None
    assert claimed.status == "PROCESSING"
    assert claimed.lease_owner == worker_a
    assert claimed.lease_until is not None
    assert service.claim(job.id, worker_b) is None


def test_expired_lease_recovery_after_worker_crash(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]
    service.claim(job.id, uuid4())
    stored = session.get(DeliveryJob, job.id)
    stored.status = "PROCESSING"
    stored.lease_until = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    recovered = service.claim(job.id, uuid4())
    assert recovered is not None
    assert recovered.status == "PROCESSING"
    assert recovered.lease_until > _naive(datetime.now(UTC))


def test_claim_skips_terminal_jobs(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]
    service.mark_sent(job.id, "pm")
    assert service.claim(job.id, uuid4()) is None


def test_claim_marks_campaign_running(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]
    service.claim(job.id, uuid4())
    assert session.get(Campaign, campaign_id).status == "RUNNING"


# ------------------------------------------------------------------ #
# Retry / failure classification
# ------------------------------------------------------------------ #
def test_transient_failure_schedules_backoff_and_retries(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]
    service.claim(job.id, uuid4())
    service.mark_failure(job.id, "PROVIDER_TIMEOUT", "timeout", retryable=True)
    stored = session.get(DeliveryJob, job.id)
    assert stored.status == "FAILED"
    assert stored.attempt_count == 1
    assert stored.next_attempt_at is not None and stored.next_attempt_at > _naive(datetime.now(UTC))
    stored.next_attempt_at = _naive(datetime.now(UTC)) - timedelta(seconds=1)
    session.commit()
    assert service.claim(job.id, uuid4()) is not None


def test_permanent_failure_is_blocked_and_terminal(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]
    service.claim(job.id, uuid4())
    service.mark_failure(job.id, "AUTH_FAILED", "unauthorized", retryable=False)
    stored = session.get(DeliveryJob, job.id)
    assert stored.status == "BLOCKED"
    assert stored.completed_at is not None
    stored.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    session.commit()
    assert service.claim(job.id, uuid4()) is None


def test_max_attempts_makes_failure_terminal(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]

    first = service.claim(job.id, uuid4())
    assert first is not None
    service.mark_failure(first.id, "PROVIDER_ERROR", "boom", retryable=True)
    stored = session.get(DeliveryJob, job.id)
    assert stored.attempt_count == 1
    assert stored.next_attempt_at is not None
    stored.next_attempt_at = _naive(datetime.now(UTC)) - timedelta(seconds=1)
    session.commit()

    second = service.claim(job.id, uuid4())
    assert second is not None
    service.mark_failure(second.id, "PROVIDER_ERROR", "boom", retryable=True)
    stored = session.get(DeliveryJob, job.id)
    assert stored.attempt_count == 2
    assert stored.next_attempt_at is None
    assert service.claim(job.id, uuid4()) is None


def test_throttle_backoff_respects_retry_after(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]
    service.claim(job.id, uuid4())
    service.throttle(job.id, 30)
    stored = session.get(DeliveryJob, job.id)
    assert stored.status == "FAILED"
    assert stored.failure_code == "PROVIDER_THROTTLED"
    assert stored.next_attempt_at is not None
    assert stored.next_attempt_at >= _naive(datetime.now(UTC)) + timedelta(seconds=29)


# ------------------------------------------------------------------ #
# Pause / cancel semantics
# ------------------------------------------------------------------ #
def test_pause_stops_new_sends_but_not_in_flight(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]
    service.claim(job.id, uuid4())  # in-flight
    campaign = service.pause(campaign_id)
    assert campaign.status == "PAUSED"
    # In-flight PROCESSING job remains (may finish), pending would not dispatch.
    assert session.get(DeliveryJob, job.id).status == "PROCESSING"
    # Discovery excludes the paused campaign entirely.
    assert service.discover_due(datetime.now(UTC)) == []


def test_cancel_marks_pending_jobs_cancelled(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    service.materialize_for_campaign(campaign_id)
    cancelled = service.cancel_pending(campaign_id)
    assert cancelled == 1
    job = session.query(DeliveryJob).filter(DeliveryJob.campaign_id == campaign_id).one()
    assert job.status == "CANCELLED"
    assert session.get(Campaign, campaign_id).status == "CANCELLED"


def test_resume_from_pause(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    service.materialize_for_campaign(campaign_id)
    assert service.pause(campaign_id).status == "PAUSED"
    assert service.resume(campaign_id).status == "SCHEDULED"


# ------------------------------------------------------------------ #
# Progress / completion
# ------------------------------------------------------------------ #
def test_progress_counts_by_status(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]
    service.claim(job.id, uuid4())
    service.mark_sent(job.id, "pm")
    counts = service.progress(campaign_id)
    assert counts["SENT"] == 1
    assert counts["PENDING"] == 0


def test_complete_campaign_when_all_jobs_terminal(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]
    service.claim(job.id, uuid4())
    service.mark_sent(job.id, "pm")
    campaign = service.mark_completed_if_done(campaign_id)
    assert campaign is not None and campaign.status == "COMPLETED"


# ------------------------------------------------------------------ #
# Discovery
# ------------------------------------------------------------------ #
def test_discovery_returns_only_due_jobs(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]
    due = service.discover_due(datetime.now(UTC))
    assert [j.id for j in due] == [job.id]
    service.pause(campaign_id)
    assert service.discover_due(datetime.now(UTC)) == []


# ------------------------------------------------------------------ #
# Tenant isolation
# ------------------------------------------------------------------ #
def test_service_is_tenant_scoped(dj_session) -> None:
    session, _, campaign_id, _, _ = dj_session
    with pytest.raises(DeliveryJobNotFoundError):
        _service(session, uuid4()).progress(campaign_id)


# ------------------------------------------------------------------ #
# Worker orchestration (fake sending service)
# ------------------------------------------------------------------ #
def test_worker_success_marks_sent(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]

    class Send:
        def send_delivery_job(self, _job_id) -> _SentMessage:
            return _SentMessage("pm-x")

    status = _run_worker(session, tenant_id, job.id, Send())
    assert status == "SENT"
    stored = session.get(DeliveryJob, job.id)
    assert stored.status == "SENT"
    assert stored.provider_message_id == "pm-x"
    assert stored.completed_at is not None


def test_worker_blocked_on_compliance(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]

    class Send:
        def send_delivery_job(self, _job_id) -> _SentMessage:
            raise SendBlockedError(object())

    status = _run_worker(session, tenant_id, job.id, Send())
    assert status == "BLOCKED"


def test_suppressed_recipient_is_blocked_right_before_sending(dj_session) -> None:
    session, tenant_id, campaign_id, _, contact_id = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]
    contact = session.get(Contact, contact_id)
    session.add(
        SuppressionEntry(
            tenant_id=tenant_id,
            email_normalized=contact.email.lower(),
            type="MANUAL",
            source="test",
            active=True,
        )
    )
    session.commit()
    service.claim(job.id, uuid4())
    sending = SendingService(session, tenant_id, lambda _sender: object())
    with pytest.raises(SendBlockedError) as error:
        sending.send_delivery_job(job.id)
    assert any(check.name == "suppression" for check in error.value.result.failures)
    stored = session.get(DeliveryJob, job.id)
    assert stored.status == "BLOCKED"
    assert stored.failure_code == "COMPLIANCE_BLOCKED"


def test_worker_throttled_defers(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]

    class Send:
        def send_delivery_job(self, _job_id) -> _SentMessage:
            raise SenderThrottledError(retry_after=30)

    status = _run_worker(session, tenant_id, job.id, Send())
    assert status == "DEFERRED"
    assert session.get(DeliveryJob, job.id).status == "FAILED"


def test_worker_transient_failure_retries(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]

    class Send:
        def send_delivery_job(self, _job_id) -> _SentMessage:
            raise TimeoutError()

    status = _run_worker(session, tenant_id, job.id, Send())
    assert status == "FAILED"
    assert session.get(DeliveryJob, job.id).failure_code == "PROVIDER_TIMEOUT"


def test_worker_permanent_sender_failure_blocks(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]

    class Send:
        def send_delivery_job(self, _job_id) -> _SentMessage:
            raise SenderUnavailableError()

    status = _run_worker(session, tenant_id, job.id, Send())
    assert status == "BLOCKED"
    assert session.get(DeliveryJob, job.id).status == "BLOCKED"


def test_duplicate_send_prevention_after_completion(dj_session) -> None:
    session, tenant_id, campaign_id, _, _ = dj_session
    service = _service(session, tenant_id)
    job = service.materialize_for_campaign(campaign_id)[0]
    service.mark_sent(job.id, "pm")
    # A second claim is impossible (terminal), so no duplicate send can occur.
    assert service.claim(job.id, uuid4()) is None
