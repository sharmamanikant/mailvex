from __future__ import annotations

import os
import time
from datetime import UTC, datetime
from uuid import UUID

from celery import Celery
from celery.signals import heartbeat_sent
from redis import Redis
from sqlalchemy import and_, or_, select

from app.core.config import settings
from app.core.database import SessionLocal
from app.core.logging import log_event
from app.models import (
    AlertRecord,
    Campaign,
    Contact,
    DeliveryJob,
    ScheduledMessage,
    SenderAccount,
)
from app.observability.alerts import AlertService
from app.observability.metrics import get_metrics
from app.observability.ops import OpsService
from app.services.delivery_jobs import DeliveryJobService
from app.services.scheduler import SchedulerService
from app.services.sending import SendingService
from app.workers.delivery_jobs import DeliveryJobWorker
from app.workers.scheduler import ScheduledMessageWorker

celery_app = Celery("crcrm_scheduler", broker=os.getenv("REDIS_URL", "redis://localhost:6379/0"), backend=os.getenv("REDIS_URL", "redis://localhost:6379/0"))
celery_app.conf.task_routes = {
    "crcrm.send_warmup_message": {"queue": "warmup"},
}
celery_app.conf.beat_schedule = {
    "dispatch-due-scheduled-messages": {
        "task": "crcrm.dispatch_due_scheduled_messages",
        "schedule": 30.0,
    },
    "dispatch-due-delivery-jobs": {
        "task": "crcrm.dispatch_due_delivery_jobs",
        "schedule": 15.0,
    },
    "drive-warmup-schedule": {
        "task": "crcrm.drive_warmup_schedule",
        "schedule": 45.0,
    },
    "sample-ops-metrics": {
        "task": "crcrm.sample_ops_metrics",
        "schedule": 30.0,
    },
    "evaluate-ops-alerts": {
        "task": "crcrm.evaluate_ops_alerts",
        "schedule": 60.0,
    },
    "sweep-stale-import-files": {
        "task": "crcrm.sweep_stale_import_files",
        "schedule": 3600.0,
    },
    "safety-review-scan": {
        "task": "crcrm.safety_review_scan",
        "schedule": 300.0,
    },
    "refresh-provider-credentials": {
        "task": "crcrm.refresh_provider_credentials",
        "schedule": 300.0,
    },
    "seed-validation-rules": {
        "task": "crcrm.seed_validation_rules",
        "schedule": 3600.0,
    },
    "reclaim-stale-imports": {
        "task": "crcrm.reclaim_stale_imports",
        "schedule": float(os.getenv("IMPORT_RECLAIM_INTERVAL_SECONDS", "60")),
    },
}

metrics = get_metrics()


def _redis() -> Redis:
    return Redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )


@heartbeat_sent.connect
def worker_heartbeat(**_kwargs: object) -> None:
    _worker_heartbeat()


def _worker_heartbeat() -> None:
    _redis().set("crcrm:heartbeat:worker", datetime.now(UTC).isoformat(), ex=120)


def _scheduler_heartbeat() -> None:
    _redis().set("crcrm:heartbeat:scheduler", datetime.now(UTC).isoformat(), ex=120)


@celery_app.task(name="crcrm.dispatch_due_scheduled_messages")
def dispatch_due_scheduled_messages() -> int:
    """Enqueue due messages from the durable database-backed schedule."""
    now = datetime.now(UTC)
    with SessionLocal() as session:
        _scheduler_heartbeat()
        items = list(
            session.scalars(
                select(ScheduledMessage)
                .where(
                    ScheduledMessage.status.in_(["QUEUED", "DEFERRED"]),
                    ScheduledMessage.due_at <= now,
                    or_(
                        ScheduledMessage.deferred_until.is_(None),
                        ScheduledMessage.deferred_until <= now,
                    ),
                )
                .order_by(
                    ScheduledMessage.priority.desc(),
                    ScheduledMessage.due_at,
                )
                .limit(100)
            ).all()
        )
        for item in items:
            schedule_message.delay(str(item.tenant_id), str(item.id))
        metrics.gauge("queue.scheduled.due", len(items))
        metrics.counter("scheduler.dispatched", amount=len(items), queue="scheduled")
        if items:
            log_event("scheduler.dispatch", queue="scheduled", count=len(items))
        return len(items)


@celery_app.task(name="crcrm.schedule_message")
def schedule_message(tenant_id: str, scheduled_id: str) -> str:
    """Claim and send one scheduled message through the normal worker path."""
    started = time.perf_counter()
    with SessionLocal() as session:
        _worker_heartbeat()
        tenant = UUID(tenant_id)
        service = SchedulerService(session, tenant)
        item = service._scheduled(UUID(scheduled_id))
        if item.status not in {"QUEUED", "DEFERRED"}:
            return item.status
        if item.deferred_until is not None and item.deferred_until > datetime.now(UTC):
            return "DEFERRED"
        status = ScheduledMessageWorker(
            service,
            SendingService(session, tenant),
        ).process(item.id)
        metrics.counter("jobs.scheduled", status=status)
        metrics.record_latency("jobs.scheduled.latency", time.perf_counter() - started, status=status)
        log_event("jobs.scheduled.completed", status=status, tenant_id=tenant_id)
        return status


@celery_app.task(name="crcrm.dispatch_due_delivery_jobs")
def dispatch_due_delivery_jobs() -> int:
    """Discover due delivery jobs and enqueue a worker task for each.

    Discovery is tenant-agnostic and driven by the (status, next_attempt_at)
    index so it never does a full-table scan. Only active campaigns (SCHEDULED,
    RUNNING, APPROVED) are considered, so paused/cancelled campaigns produce no
    new sends.
    """
    now = datetime.now(UTC)
    with SessionLocal() as session:
        _scheduler_heartbeat()
        active_campaigns = select(Campaign.id).where(
            Campaign.status.in_({"SCHEDULED", "RUNNING", "APPROVED"})
        )
        due = list(
            session.scalars(
                select(DeliveryJob)
                .where(
                    DeliveryJob.campaign_id.in_(active_campaigns),
                    or_(
                        and_(
                            DeliveryJob.status == "PENDING",
                            or_(
                                DeliveryJob.next_attempt_at.is_(None),
                                DeliveryJob.next_attempt_at <= now,
                            ),
                        ),
                        and_(
                            DeliveryJob.status == "PROCESSING",
                            DeliveryJob.lease_until.is_not(None),
                            DeliveryJob.lease_until < now,
                        ),
                        and_(
                            DeliveryJob.status == "FAILED",
                            DeliveryJob.next_attempt_at.is_not(None),
                            DeliveryJob.next_attempt_at <= now,
                            DeliveryJob.attempt_count < DeliveryJob.max_attempts,
                        ),
                    ),
                )
                .order_by(DeliveryJob.scheduled_at, DeliveryJob.created_at)
                .limit(200)
            ).all()
        )
        for job in due:
            deliver_job.delay(str(job.tenant_id), str(job.id))
        metrics.gauge("queue.delivery.due", len(due))
        metrics.counter("scheduler.dispatched", amount=len(due), queue="delivery")
        if due:
            log_event("scheduler.dispatch", queue="delivery", count=len(due))
        return len(due)


@celery_app.task(name="crcrm.deliver_job")
def deliver_job(tenant_id: str, job_id: str) -> str:
    """Claim and process a single delivery job through the normal worker path."""
    started = time.perf_counter()
    with SessionLocal() as session:
        _worker_heartbeat()
        tenant = UUID(tenant_id)
        job = session.get(DeliveryJob, UUID(job_id))
        campaign_id = job.campaign_id if job is not None else None
        service = DeliveryJobService(session, tenant)
        worker = DeliveryJobWorker(service, SendingService(session, tenant))
        status = worker.process(UUID(job_id))
        refreshed = session.get(DeliveryJob, UUID(job_id))
        metrics.record_latency("jobs.delivery.latency", time.perf_counter() - started, status=status)
        metrics.counter("jobs.delivery", status=status)
        if refreshed is not None:
            if refreshed.attempt_count and refreshed.attempt_count > 1:
                metrics.counter("jobs.delivery.retried", status=status)
            if status in {"FAILED", "BLOCKED"}:
                failure_code = refreshed.failure_code or "unknown"
                metrics.counter("jobs.delivery.failures", code=failure_code, status=status)
                log_event(
                    "jobs.delivery.failed",
                    tenant_id=tenant_id,
                    job_id=job_id,
                    failure_code=failure_code,
                    status=status,
                )
        if (
            campaign_id is not None
            and status in {"SENT", "DELIVERED", "FAILED", "BLOCKED", "CANCELLED"}
        ):
            service.mark_completed_if_done(campaign_id)
        return status


def _drive_warmup(tenant_id: UUID) -> list[UUID]:
    """Drive one tenant's due warmup senders onto the warmup queue."""
    from app.security.rate_limit import RateLimitService
    from app.services.sender_health import SenderHealthService
    from app.services.sender_quotas import SenderQuotaService
    from app.services.warmup import WarmupService

    with SessionLocal() as session:
        return WarmupService(
            session,
            tenant_id,
            SenderQuotaService(RateLimitService(settings.redis_url, fail_open=False)),
            SenderHealthService(session, tenant_id),
        ).drive(lambda t, s: send_warmup_message.delay(t, s))


@celery_app.task(name="crcrm.drive_warmup_schedule")
def drive_warmup_schedule() -> list[str]:
    """Enqueue due warmup sends from Redis state onto the dedicated queue."""
    with SessionLocal() as session:
        _worker_heartbeat()
        tenant_ids = set(session.scalars(select(SenderAccount.tenant_id).distinct()).all())
    dispatched = [
        str(sender_id)
        for tenant_id in tenant_ids
        for sender_id in _drive_warmup(tenant_id)
    ]
    metrics.gauge("queue.warmup.dispatched", len(dispatched))
    if dispatched:
        log_event("scheduler.dispatch", queue="warmup", count=len(dispatched))
    return dispatched


@celery_app.task(name="crcrm.send_warmup_message")
def send_warmup_message(tenant_id: str, sender_id: str) -> str:
    """Send one warmup message and re-arm the next from the schedule."""
    from app.security.rate_limit import RateLimitService
    from app.services.sender_health import SenderHealthService
    from app.services.sender_quotas import SenderQuotaService
    from app.services.warmup import WarmupService

    started = time.perf_counter()
    with SessionLocal() as session:
        _worker_heartbeat()
        tenant = UUID(tenant_id)
        service = WarmupService(
            session,
            tenant,
            SenderQuotaService(RateLimitService(settings.redis_url, fail_open=False)),
            SenderHealthService(session, tenant),
        )
        try:
            status = service.send_message(UUID(sender_id))
        except Exception as exc:
            metrics.counter("jobs.warmup", status="BLOCKED")
            log_event("jobs.warmup.blocked", tenant_id=tenant_id, sender_id=sender_id, reason=str(exc))
            return "BLOCKED"
        metrics.counter("jobs.warmup", status=status)
        metrics.record_latency("jobs.warmup.latency", time.perf_counter() - started, status=status)
        log_event("jobs.warmup.completed", status=status, tenant_id=tenant_id, sender_id=sender_id)
        return status


@celery_app.task(name="crcrm.sample_ops_metrics")
def sample_ops_metrics() -> dict[str, float | None]:
    """Periodically measure queue depths, latencies, and heartbeat ages."""
    from app.observability.metrics import get_redis

    with SessionLocal() as session:
        _worker_heartbeat()
        # Persist a system-wide sample row (tenant_id NULL):
        values = OpsService(session, metrics, get_redis()).sample()
        return {key: value for key, value in values.items() if value is not None}


@celery_app.task(name="crcrm.evaluate_ops_alerts")
def evaluate_ops_alerts() -> dict[str, int]:
    """Run alert rules and reconcile durable alert records."""
    with SessionLocal() as session:
        _worker_heartbeat()
        service = AlertService(session, metrics)
        results = service.evaluate()
        counts = service.reconcile(results)
        counts["firing"] = len(results)
        # Keep resolved records from accumulating forever (simple retention).
        session.query(AlertRecord).filter(AlertRecord.status == "RESOLVED").delete(synchronize_session=False)
        session.commit()
        return counts


@celery_app.task(name="crcrm.sweep_stale_import_files")
def sweep_stale_import_files() -> dict[str, int]:
    """Delete uploaded import files older than the configured retention window.

    Uploaded source files are business/contact data that must not be retained
    indefinitely. This runs hourly and removes source files (and error reports)
    whose owning job reached a terminal state before the retention window.
    """
    from app.services.storage_retention import sweep_import_files

    with SessionLocal() as session:
        _worker_heartbeat()
        result = sweep_import_files(session)
        session.commit()
        if result["deleted_files"]:
            log_event("storage.retention.swept", **{k: str(v) for k, v in result.items()})
    return result


# ------------------------------------------------------------------ #
# Phase 2 — workspace mailbox sync (one-off background task)
# ------------------------------------------------------------------ #
@celery_app.task(
    name="crcrm.sync_provider_mailboxes",
    soft_time_limit=900,
    time_limit=1200,
)
def sync_provider_mailboxes(connection_id: str, tenant_id: str) -> dict[str, str]:
    """Background task: discover workspace mailboxes for a provider connection.

    The task is enqueued by ``POST /api/v1/provider-connections/:id/sync``
    (non-inline mode). It creates a fresh session, calls the sync pipeline,
    and returns counts. The sync pipeline commits the connection status +
    mailbox rows itself.

    Hard/soft time limits guarantee a hung Google Directory HTTP call cannot
    wedge a production worker forever (900s covers hundreds of pages at
    200 users/page; a 429 storm resolves via the provider backoff first).
    """
    from uuid import UUID

    from app.services.mailboxes import run_provider_mailbox_sync

    with SessionLocal() as session:
        _worker_heartbeat()
        run_provider_mailbox_sync(session, UUID(tenant_id), UUID(connection_id))
    return {"status": "completed"}


@celery_app.task(name="crcrm.safety_review_scan")
def safety_review_scan() -> int:
    """Periodic protective-pause scan over active System B senders.

    Guardrail 12: bounce/complaint/provider-error rates are re-evaluated on a
    rolling window, not only on individual events, and senders that trip a
    threshold are paused for human review. Paused senders are never resumed
    automatically; only ``release`` (explicit, audited) re-enables them.
    """
    from app.services.safety import SenderSafetyService

    paused = 0
    with SessionLocal() as session:
        _worker_heartbeat()
        accounts = session.scalars(
            select(SenderAccount).where(SenderAccount.status == "ACTIVE")
        ).all()
        for account in accounts:
            service = SenderSafetyService(session, account.tenant_id)
            state, reasons = service.evaluate_by_account(account)
            service.apply(account, state, reasons)
            if state == "REVIEW_REQUIRED":
                paused += 1
        session.commit()
    if paused:
        log_event("safety.review_scan", paused=str(paused))
    return paused


@celery_app.task(name="crcrm.refresh_provider_credentials")
def refresh_provider_credentials() -> dict[str, int]:
    """Sweep all tenants for provider connections due for credential refresh.

    Connections whose ``credential_expires_at`` is within the configured margin
    are refreshed. Permanent failures (invalid_grant → token revoked by
    Google admin) revoke the connection; transient failures are retried next
    cycle. This runs every 5 minutes via the celery beat schedule.
    """
    from app.services.provider_connections import refresh_due_provider_connections

    with SessionLocal() as session:
        _worker_heartbeat()
        result = refresh_due_provider_connections(session)
        session.commit()
    total = sum(result.values())
    if total:
        log_event(
            "provider_credential.refresh_sweep",
            refreshed=str(result["refreshed"]),
            failed=str(result["failed"]),
            revoked=str(result["revoked"]),
        )
    return result


# ------------------------------------------------------------------ #
# Contact validation engine (async, chunked)
# ------------------------------------------------------------------ #
@celery_app.task(name="crcrm.run_import_validation", soft_time_limit=1500, time_limit=1800)
def run_import_validation(job_id: str, tenant_id: str, source_file: str) -> str:
    """Validate an uploaded import file on the worker.

    Runs on the broker rather than in the API process so a deploy mid-validation
    cannot drop the job; ``reclaim_stale_imports`` re-queues it if the worker
    itself dies.
    """
    from app.workers.imports import process_validate

    _worker_heartbeat()
    process_validate(UUID(job_id), UUID(tenant_id), source_file)
    return job_id


@celery_app.task(name="crcrm.run_contact_import", soft_time_limit=7200, time_limit=7800)
def run_contact_import(
    job_id: str,
    tenant_id: str,
    source_file: str,
    mapping: dict[str, str] | None = None,
    policy: str = "SKIP",
) -> str:
    """Process a validated import file on the worker.

    Progress is committed in batches, so a reclaim after a dead worker resumes
    rather than restarts, and duplicate policies keep the replay idempotent.
    """
    from app.workers.imports import process_import

    _worker_heartbeat()
    process_import(UUID(job_id), UUID(tenant_id), source_file, mapping, policy)
    return job_id


@celery_app.task(name="crcrm.reclaim_stale_imports", soft_time_limit=120, time_limit=180)
def reclaim_stale_imports() -> dict[str, int]:
    """Re-queue imports whose worker heartbeat stopped.

    Without this, a job interrupted by a crashed container stays VALIDATING or
    IMPORTING forever and the UI can never resolve it to a terminal state.
    """
    from app.workers.imports import reclaim_stale_imports as reclaim

    with SessionLocal() as session:
        _worker_heartbeat()
        result = reclaim(session)
    if result["requeued"]:
        log_event("storage.imports.reclaimed", **result)
    return result


@celery_app.task(name="crcrm.seed_validation_rules", soft_time_limit=120, time_limit=180)
def seed_validation_rules() -> dict[str, int]:
    """Load the curated disposable / free-provider / role rules into the database.

    Idempotent and conflict-safe, so running it on a schedule both bootstraps a
    fresh install and adds newly shipped seed rules without overwriting any
    operator edit already stored in the tables.
    """
    from app.services.email_validation import seed_validation_rules as seed

    with SessionLocal() as session:
        _worker_heartbeat()
        inserted = seed(session)
    log_event("validation.rules.seeded", inserted=inserted)
    return {"inserted": inserted}


@celery_app.task(name="crcrm.verify_contact", soft_time_limit=120, time_limit=180)
def verify_contact(tenant_id: str, contact_id: str) -> dict[str, object]:
    """Verify a single contact outside any HTTP request.

    Validation performs DNS lookups and (optionally) SMTP probes, so it must
    never run inside a request. A hard/soft time limit guarantees a hung
    resolver cannot wedge a worker slot indefinitely.
    """
    from uuid import UUID

    from app.services.verification import ContactVerifier

    with SessionLocal() as session:
        _worker_heartbeat()
        tenant_uuid = UUID(tenant_id)
        contact = session.scalar(
            select(Contact).where(
                Contact.id == UUID(contact_id), Contact.tenant_id == tenant_uuid
            )
        )
        if contact is None:
            return {"status": "not_found"}
        verdict = ContactVerifier(session, tenant_uuid).verify(contact)
        session.commit()
        return verdict.as_dict()


@celery_app.task(name="crcrm.run_verification_job", soft_time_limit=3300, time_limit=3600)
def run_verification_job(tenant_id: str, job_id: str) -> dict[str, object]:
    """Drive a bulk verification job to completion, one chunk at a time.

    Each iteration pulls at most ``VERIFICATION_CHUNK_SIZE`` contacts via a
    keyset cursor and commits that slice, so peak memory is bounded regardless
    of tenant size and a crash mid-run leaves completed chunks committed rather
    than rolling the whole job back.

    A single unparseable contact must not abandon a million-row run, so
    verification errors are counted per contact and the loop continues. Any
    failure that does end the job is still recorded as ``FAILED`` rather than
    leaving the row stuck in ``RUNNING`` forever.
    """
    import logging as _logging
    from uuid import UUID

    from app.core.config import settings
    from app.services.verification import ContactVerifier
    from app.services.verification_jobs import VerificationJobService

    task_log = _logging.getLogger(__name__)

    processed = 0
    with SessionLocal() as session:
        _worker_heartbeat()
        tenant_uuid = UUID(tenant_id)
        jobs = VerificationJobService(session, tenant_uuid)
        try:
            job = jobs.claim(UUID(job_id))
            if job is None:
                return {"status": "skipped"}
            probe_smtp = bool((job.request_payload or {}).get("probe_smtp"))
            while True:
                batch = jobs.next_batch(job)
                if not batch:
                    break
                verifier = ContactVerifier(session, tenant_uuid)
                verdicts = []
                failed = 0
                for contact in batch:
                    try:
                        verdicts.append(verifier.verify(contact, probe_smtp=probe_smtp))
                    except Exception as exc:
                        failed += 1
                        task_log.warning(
                            "contact verification failed contact=%s error=%s", contact.id, exc
                        )
                        session.rollback()
                session.commit()
                jobs.record(job, verdicts, failed=failed)
                processed += len(batch)
                _worker_heartbeat()
                if jobs.is_cancelled(job):
                    return {"status": job.status, "processed": processed}
                if len(batch) < settings.verification_chunk_size:
                    break
            jobs.finish(job)
            return {"status": job.status, "processed": processed}
        except Exception as exc:
            session.rollback()
            task_log.exception("verification job %s aborted", job_id)
            try:
                job = jobs.claim(UUID(job_id))
                if job is not None:
                    jobs.finish(job, error=str(exc)[:500])
            except Exception:
                session.rollback()
            raise
