from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import ColumnElement, and_, func, or_, select, update
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.models import (
    Campaign,
    CampaignRecipient,
    CampaignSender,
    CampaignVersion,
    Contact,
    DeliveryJob,
    EmailAccount,
    SenderAccount,
)
from app.services.audit import AuditService
from app.services.suppression_engine import SuppressionEngine

JOB_PENDING = "PENDING"
JOB_PROCESSING = "PROCESSING"
JOB_SENT = "SENT"
JOB_DELIVERED = "DELIVERED"
JOB_FAILED = "FAILED"
JOB_BLOCKED = "BLOCKED"
JOB_CANCELLED = "CANCELLED"

JOB_RETRYABLE = {JOB_FAILED}
JOB_WORKING = {JOB_PENDING, JOB_PROCESSING}
JOB_TERMINAL = {JOB_SENT, JOB_DELIVERED, JOB_FAILED, JOB_BLOCKED, JOB_CANCELLED}
JOB_ALL = JOB_WORKING | JOB_TERMINAL

# Transient provider failures (retry with exponential backoff). Everything else
# is treated as permanent and terminal. Provider throttling is handled separately
# via ``SenderThrottledError`` / the ``throttle`` method.
TRANSIENT_FAILURE_CODES = {
    "PROVIDER_TIMEOUT",
    "PROVIDER_UNAVAILABLE",
    "NETWORK_ERROR",
    "TEMPORARY_REJECTION",
    "SMTP_TRANSIENT",
}

RETRY_BASE_SECONDS = 60
RETRY_MAX_SECONDS = 3600


class DeliveryJobError(ValueError):
    pass


class DeliveryJobNotFoundError(LookupError):
    pass


class DeliveryJobService:
    """Durable, concurrency-safe delivery queue built on ``delivery_jobs``.

    Idempotency is enforced by the DB level unique constraint
    (tenant_id, campaign_id, recipient_id); claiming is a single atomic
    ``UPDATE ... WHERE ... RETURNING`` so only one worker ever wins a job.
    """

    def __init__(self, session: Session, tenant_id: UUID, lease_seconds: int = 300) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.lease_seconds = lease_seconds

    # ------------------------------------------------------------------ #
    # Materialization (idempotent)
    # ------------------------------------------------------------------ #
    def materialize_for_campaign(self, campaign_id: UUID) -> list[DeliveryJob]:
        campaign = self._campaign(campaign_id)
        if campaign.status in {"DRAFT", "REVIEW", "CANCELLED", "COMPLETED"}:
            raise DeliveryJobError(
                f"Campaign cannot be scheduled from state {campaign.status}"
            )
        rows = list(
            self.session.scalars(
                select(CampaignRecipient).where(
                    CampaignRecipient.tenant_id == self.tenant_id,
                    CampaignRecipient.campaign_id == campaign.id,
                )
            ).all()
        )
        from app.services.sender_pool import SenderPoolService

        pool_rows = list(
            self.session.scalars(
                select(CampaignSender).where(
                    CampaignSender.tenant_id == self.tenant_id,
                    CampaignSender.campaign_id == campaign.id,
                )
            ).all()
        )
        sender = self.session.scalar(
            select(EmailAccount).where(
                EmailAccount.tenant_id == self.tenant_id,
                EmailAccount.id == campaign.sender_id,
            )
        )
        pool: list[SenderAccount] = []
        if pool_rows:
            pool = SenderPoolService(self.session, self.tenant_id).rotation(campaign.id)
            if not pool:
                raise DeliveryJobError("Campaign sender pool has no usable senders")
            if sender is None:
                raise DeliveryJobError("Sender is not available for scheduling")
        else:
            if sender is None or sender.status in {"DISABLED", "HEALTH_CRITICAL"}:
                raise DeliveryJobError("Sender is not available for scheduling")
            if sender.status not in {"CONNECTED", "HEALTH_WARNING"}:
                raise DeliveryJobError("Sender health is not acceptable for scheduling")
        version = self._latest_version(campaign.id)
        suppression = SuppressionEngine(self.session, self.tenant_id)
        jobs: list[DeliveryJob] = []
        for index, row in enumerate(rows):
            contact = self.session.scalar(
                select(Contact).where(
                    Contact.id == row.contact_id,
                    Contact.tenant_id == self.tenant_id,
                )
            )
            if contact is None:
                continue
            if suppression.is_suppressed(contact.email):
                # Suppression is checked at scheduling time so no delivery job is
                # created for an already-suppressed recipient. The send-time
                # compliance gate remains the authoritative backstop.
                continue
            scheduled_at = self._start_time(campaign, index)
            existing = self.session.scalar(
                select(DeliveryJob).where(
                    DeliveryJob.tenant_id == self.tenant_id,
                    DeliveryJob.campaign_id == campaign.id,
                    DeliveryJob.recipient_id == contact.id,
                )
            )
            if existing is not None:
                # Durable idempotency: re-claim an existing non-terminal job
                # instead of duplicating rows.
                if existing.status in JOB_TERMINAL and existing.status not in {
                    JOB_FAILED,
                    JOB_BLOCKED,
                }:
                    jobs.append(existing)
                    continue
                jobs.append(existing)
                continue
            jobs.append(
                DeliveryJob(
                    tenant_id=self.tenant_id,
                    campaign_id=campaign.id,
                    campaign_version_id=version.id if version else None,
                    recipient_id=contact.id,
sender_id=sender.id,
                sender_account_id=pool[index % len(pool)].id if pool else None,
                    scheduled_at=scheduled_at,
                    status=JOB_PENDING,
                    attempt_count=0,
                    max_attempts=int(campaign.schedule_config.get("max_attempts", 5)),
                    next_attempt_at=scheduled_at,
                )
            )
        self.session.add_all(jobs)
        campaign.status = "SCHEDULED"
        AuditService(self.session, self.tenant_id).record(
            "CAMPAIGN_SCHEDULED", "campaign", campaign.id, {"job_count": len(jobs)}
        )
        self.session.commit()
        return jobs

    # ------------------------------------------------------------------ #
    # Concurrency-safe claiming
    # ------------------------------------------------------------------ #
    def _claim_conditions(self, now: datetime) -> ColumnElement[bool]:
        return or_(
            and_(
                DeliveryJob.status == JOB_PENDING,
                or_(
                    DeliveryJob.next_attempt_at.is_(None),
                    DeliveryJob.next_attempt_at <= now,
                ),
            ),
            # Expired-lease recovery: a worker crashed while processing; the
            # lease lapsed so another worker may reclaim the job.
            and_(
                DeliveryJob.status == JOB_PROCESSING,
                DeliveryJob.lease_until.is_not(None),
                DeliveryJob.lease_until < now,
            ),
            # Retryable transient failure whose backoff window has elapsed.
            and_(
                DeliveryJob.status == JOB_FAILED,
                DeliveryJob.next_attempt_at.is_not(None),
                DeliveryJob.next_attempt_at <= now,
                DeliveryJob.attempt_count < DeliveryJob.max_attempts,
            ),
        )

    def claim(self, job_id: UUID, worker_id: UUID | None = None) -> DeliveryJob | None:
        """Atomically claim a job. Returns the job or None if not claimable.

        Single conditional UPDATE guarantees exclusive ownership even with many
        concurrent workers (only one UPDATE can transition the row).
        """
        now = datetime.now(UTC)
        owner = worker_id or uuid4()
        result = self.session.execute(
            update(DeliveryJob)
            .where(
                DeliveryJob.id == job_id,
                DeliveryJob.tenant_id == self.tenant_id,
                self._claim_conditions(now),
            )
            .values(
                status=JOB_PROCESSING,
                lease_owner=owner,
                lease_until=now + timedelta(seconds=self.lease_seconds),
                processing_started_at=now,
                last_attempt_at=now,
            )
            .returning(DeliveryJob)
            .execution_options(synchronize_session=False)
        )
        job = result.scalars().first()
        self.session.commit()
        if job is None:
            return None
        self._mark_campaign_running(job.campaign_id)
        return job
    def claim_set(self, job_ids: list[UUID], worker_id: UUID | None = None) -> list[DeliveryJob]:
        claimed: list[DeliveryJob] = []
        for job_id in job_ids:
            job = self.claim(job_id, worker_id)
            if job is not None:
                claimed.append(job)
        return claimed

    # ------------------------------------------------------------------ #
    # Outcome transitions
    # ------------------------------------------------------------------ #
    def mark_sent(self, job_id: UUID, provider_message_id: str | None = None) -> DeliveryJob:
        job = self._owned(job_id)
        job.status = JOB_SENT
        job.provider_message_id = provider_message_id
        job.failure_code = None
        job.last_error = None
        job.completed_at = datetime.now(UTC)
        self._clear_lease(job)
        self.session.commit()
        return job

    def mark_delivered(self, job_id: UUID, provider_message_id: str | None = None) -> DeliveryJob:
        job = self._owned(job_id)
        job.status = JOB_DELIVERED
        job.provider_message_id = provider_message_id
        job.failure_code = None
        job.last_error = None
        job.completed_at = datetime.now(UTC)
        self._clear_lease(job)
        self.session.commit()
        return job

    def mark_failure(
        self,
        job_id: UUID,
        code: str,
        error: str,
        retryable: bool = True,
    ) -> DeliveryJob:
        job = self._owned(job_id)
        now = datetime.now(UTC)
        job.attempt_count += 1
        if not retryable or job.attempt_count >= job.max_attempts:
            status = JOB_BLOCKED if not retryable else JOB_FAILED
            job.status = status
            job.next_attempt_at = None
            job.failure_code = code
            job.last_error = error
            job.completed_at = now
            self._clear_lease(job)
            self.session.commit()
            return job
        backoff = min(
            RETRY_MAX_SECONDS, RETRY_BASE_SECONDS * (2 ** (job.attempt_count - 1))
        )
        job.status = JOB_FAILED
        job.next_attempt_at = now + timedelta(seconds=backoff)
        job.failure_code = code
        job.last_error = error
        job.completed_at = None
        self._clear_lease(job)
        self.session.commit()
        return job

    def mark_blocked(self, job_id: UUID, code: str, error: str) -> DeliveryJob:
        return self.mark_failure(job_id, code, error, retryable=False)

    def throttle(self, job_id: UUID, retry_after: int) -> DeliveryJob:
        """Provider throttling backoff. Never circumvents the limit; the job is
        rescheduled strictly after ``retry_after`` seconds."""
        job = self._owned(job_id)
        now = datetime.now(UTC)
        job.attempt_count += 1
        job.status = JOB_FAILED
        job.next_attempt_at = now + timedelta(seconds=max(0, int(retry_after)))
        job.failure_code = "PROVIDER_THROTTLED"
        job.last_error = "Provider throttled delivery"
        job.completed_at = None
        self._clear_lease(job)
        self.session.commit()
        return job

    def cancel_pending(self, campaign_id: UUID, reason: str = "Campaign cancelled") -> int:
        campaign = self._campaign(campaign_id)
        if campaign.status in {"COMPLETED", "CANCELLED"}:
            raise DeliveryJobError("Campaign is already terminal")
        campaign.status = "CANCELLED"
        result = self.session.execute(
            update(DeliveryJob)
            .where(
                DeliveryJob.tenant_id == self.tenant_id,
                DeliveryJob.campaign_id == campaign.id,
                DeliveryJob.status.in_(JOB_WORKING),
            )
            .values(status=JOB_CANCELLED, failure_code="CANCELLED", last_error=reason, completed_at=datetime.now(UTC))
        )
        self.session.commit()
        return cast(CursorResult[Any], result).rowcount or 0

    def pause(self, campaign_id: UUID) -> Campaign:
        campaign = self._campaign(campaign_id)
        if campaign.status not in {"SCHEDULED", "RUNNING"}:
            raise DeliveryJobError("Campaign cannot be paused from its current state")
        # Pause = no new sends. Already claimed/in-flight jobs may finish.
        campaign.status = "PAUSED"
        self.session.commit()
        return campaign

    def resume(self, campaign_id: UUID) -> Campaign:
        campaign = self._campaign(campaign_id)
        if campaign.status != "PAUSED":
            raise DeliveryJobError("Only paused campaigns can resume")
        campaign.status = "SCHEDULED"
        self.session.commit()
        return campaign

    def send_now(self, campaign_id: UUID) -> list[DeliveryJob]:
        """Send immediately: make every pending job due right now."""
        campaign = self._campaign(campaign_id)
        if campaign.status not in {"SCHEDULED", "RUNNING", "APPROVED"}:
            raise DeliveryJobError("Campaign is not schedulable")
        now = datetime.now(UTC)
        self.session.execute(
            update(DeliveryJob)
            .where(
                DeliveryJob.tenant_id == self.tenant_id,
                DeliveryJob.campaign_id == campaign.id,
                DeliveryJob.status == JOB_PENDING,
            )
            .values(next_attempt_at=now)
        )
        campaign.status = "SCHEDULED"
        self.session.commit()
        return list(
            self.session.scalars(
                select(DeliveryJob).where(
                    DeliveryJob.tenant_id == self.tenant_id,
                    DeliveryJob.campaign_id == campaign.id,
                    DeliveryJob.status == JOB_PENDING,
                )
            ).all()
        )

    def mark_completed_if_done(self, campaign_id: UUID) -> Campaign | None:
        """Transition the campaign to COMPLETED when no jobs remain working."""
        campaign = self._campaign(campaign_id)
        if campaign.status in {"CANCELLED", "COMPLETED"}:
            return campaign
        if not self.all_terminal(campaign.id):
            return None
        if campaign.status in {"SCHEDULED", "RUNNING"}:
            campaign.status = "COMPLETED"
            AuditService(self.session, self.tenant_id).record(
                "CAMPAIGN_COMPLETED", "campaign", campaign.id
            )
            self.session.commit()
        return campaign

    def mark_campaign_completed(self, campaign_id: UUID) -> Campaign:
        campaign = self._campaign(campaign_id)
        if campaign.status in {"CANCELLED", "COMPLETED"}:
            return campaign
        campaign.status = "COMPLETED"
        self.session.commit()
        return campaign

    # ------------------------------------------------------------------ #
    # Progress / statistics
    # ------------------------------------------------------------------ #
    def progress(self, campaign_id: UUID) -> dict[str, int]:
        campaign = self._campaign(campaign_id)
        rows = self.session.execute(
            select(DeliveryJob.status, func.count(DeliveryJob.id))
            .where(
                DeliveryJob.tenant_id == self.tenant_id,
                DeliveryJob.campaign_id == campaign.id,
            )
            .group_by(DeliveryJob.status)
        ).all()
        counts: dict[str, int] = {status: 0 for status in JOB_ALL}
        for status, count in rows:
            counts[status] = int(count)
        return counts

    def all_terminal(self, campaign_id: UUID) -> bool:
        outstanding = self.session.scalar(
            select(func.count(DeliveryJob.id)).where(
                DeliveryJob.tenant_id == self.tenant_id,
                DeliveryJob.campaign_id == campaign_id,
                DeliveryJob.status.in_(JOB_WORKING),
            )
        )
        return (outstanding or 0) == 0

    # ------------------------------------------------------------------ #
    # Scheduler discovery (indexed)
    # ------------------------------------------------------------------ #
    def discover_due(self, now: datetime, limit: int = 100) -> list[DeliveryJob]:
        """Return job rows reduced to the work queue, indexed on
        (status, next_attempt_at). Skips PAUSED/CANCELLED campaigns so paused
        campaigns generate no new sends."""
        active_campaigns = select(Campaign.id).where(
            Campaign.tenant_id == self.tenant_id,
            Campaign.status.in_({"SCHEDULED", "RUNNING", "APPROVED"}),
        )
        return list(
            self.session.scalars(
                select(DeliveryJob)
                .where(
                    DeliveryJob.tenant_id == self.tenant_id,
                    DeliveryJob.campaign_id.in_(active_campaigns),
                    self._claim_conditions(now),
                )
                .order_by(DeliveryJob.scheduled_at, DeliveryJob.created_at)
                .limit(limit)
            ).all()
        )

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _campaign(self, campaign_id: UUID) -> Campaign:
        campaign = self.session.scalar(
            select(Campaign).where(
                Campaign.id == campaign_id,
                Campaign.tenant_id == self.tenant_id,
            )
        )
        if campaign is None:
            raise DeliveryJobNotFoundError("Campaign not found")
        return campaign

    def campaign(self, campaign_id: UUID) -> Campaign:
        return self._campaign(campaign_id)

    def _owned(self, job_id: UUID) -> DeliveryJob:
        job = self.session.scalar(
            select(DeliveryJob).where(
                DeliveryJob.id == job_id,
                DeliveryJob.tenant_id == self.tenant_id,
            )
        )
        if job is None:
            raise DeliveryJobNotFoundError("Delivery job not found")
        return job

    def _latest_version(self, campaign_id: UUID) -> CampaignVersion | None:
        return self.session.scalar(
            select(CampaignVersion)
            .where(
                CampaignVersion.tenant_id == self.tenant_id,
                CampaignVersion.campaign_id == campaign_id,
            )
            .order_by(CampaignVersion.version_number.desc())
            .limit(1)
        )

    @staticmethod
    def _clear_lease(job: DeliveryJob) -> None:
        job.lease_owner = None
        job.lease_until = None

    def _mark_campaign_running(self, campaign_id: UUID) -> None:
        campaign = self.session.scalar(
            select(Campaign).where(
                Campaign.id == campaign_id,
                Campaign.tenant_id == self.tenant_id,
            )
        )
        if campaign is not None and campaign.status in {"SCHEDULED", "APPROVED"}:
            campaign.status = "RUNNING"
            AuditService(self.session, self.tenant_id).record(
                "CAMPAIGN_RUNNING", "campaign", campaign.id
            )
            self.session.commit()

    @staticmethod
    def _start_time(campaign: Campaign, offset: int = 0) -> datetime:
        configured = campaign.schedule_config.get("start_at")
        if configured:
            value = datetime.fromisoformat(str(configured).replace("Z", "+00:00"))
            if value.tzinfo is None:
                try:
                    value = value.replace(
                        tzinfo=ZoneInfo(str(campaign.schedule_config.get("timezone_policy") or "UTC"))
                    )
                except ZoneInfoNotFoundError:
                    value = value.replace(tzinfo=UTC)
            return (value + timedelta(minutes=offset)).astimezone(UTC)
        return (datetime.now(UTC) + timedelta(minutes=offset)).astimezone(UTC)
