"""Alert evaluation and lifecycle for operations.

Rules are data-driven: each rule inspects a lightweight ``state`` snapshot
(produced by :func:`collect_state`) and returns a firing message when its
condition holds. The service reconciles firing rules against durable
``AlertRecord`` rows (OPEN -> ACKNOWLEDGED -> RESOLVED).
"""

from __future__ import annotations

import shutil
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.logging import log_event
from app.models import AlertRecord, DeliveryJob, EmailAccount, Webhook
from app.observability.metrics import MetricsService, NoOpMetrics
from app.services.readiness import readiness

RECENT_WINDOW = timedelta(minutes=15)

# Default thresholds (overridable per-call so tests can exercise the rules
# without fabricating hundreds of rows).
DEFAULT_QUEUE_GROWING_JOBS = 250
DEFAULT_PROVIDER_FAILURE_RATE = 0.10
DEFAULT_MIN_PROVIDER_ATTEMPTS = 20
DEFAULT_DISK_MIN_FREE_BYTES = 500 * 1024 * 1024
DEFAULT_DISK_MIN_FREE_FRACTION = 0.05
DEFAULT_CRITICAL_SENDER_LIMIT = 1


@dataclass(frozen=True)
class AlertResult:
    rule: str
    label: str
    severity: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class AlertRuleSpec:
    name: str
    label: str
    severity: str
    evaluate: Callable[[dict[str, Any]], AlertResult | None]


class AlertService:
    """Evaluates ops rules and reconciles durable alert records."""

    def __init__(
        self,
        session: Session,
        metrics: MetricsService | NoOpMetrics,
        *,
        queue_growing_jobs: int = DEFAULT_QUEUE_GROWING_JOBS,
        provider_failure_rate: float = DEFAULT_PROVIDER_FAILURE_RATE,
        min_provider_attempts: int = DEFAULT_MIN_PROVIDER_ATTEMPTS,
        disk_min_free_bytes: int = DEFAULT_DISK_MIN_FREE_BYTES,
        disk_min_free_fraction: float = DEFAULT_DISK_MIN_FREE_FRACTION,
        critical_sender_limit: int = DEFAULT_CRITICAL_SENDER_LIMIT,
    ) -> None:
        self.session = session
        self.metrics = metrics
        self.queue_growing_jobs = queue_growing_jobs
        self.provider_failure_rate = provider_failure_rate
        self.min_provider_attempts = min_provider_attempts
        self.disk_min_free_bytes = disk_min_free_bytes
        self.disk_min_free_fraction = disk_min_free_fraction
        self.critical_sender_limit = critical_sender_limit

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #
    def _provider_outcomes(self) -> tuple[int, int]:
        window = datetime.now(UTC) - RECENT_WINDOW
        failed = self.session.scalar(
            select(func.count())
            .select_from(DeliveryJob)
            .where(DeliveryJob.status == "FAILED", DeliveryJob.updated_at >= window)
        )
        succeeded = self.session.scalar(
            select(func.count())
            .select_from(DeliveryJob)
            .where(DeliveryJob.status.in_(["SENT", "DELIVERED"]), DeliveryJob.updated_at >= window)
        )
        return int(failed or 0), int(succeeded or 0)

    def _queue_backlog(self) -> int:
        now = datetime.now(UTC)
        count = self.session.scalar(
            select(func.count())
            .select_from(DeliveryJob)
            .where(
                DeliveryJob.status.in_(["PENDING", "PROCESSING"]),
                (DeliveryJob.next_attempt_at.is_(None)) | (DeliveryJob.next_attempt_at <= now),
            )
        )
        return int(count or 0)

    def _webhook_failures(self) -> int:
        window = datetime.now(UTC) - RECENT_WINDOW
        count = self.session.scalar(
            select(func.count())
            .select_from(Webhook)
            .where(Webhook.signature_valid.is_(False), Webhook.created_at >= window)
        )
        return int(count or 0)

    def _critical_senders(self) -> int:
        count = self.session.scalar(
            select(func.count()).select_from(EmailAccount).where(
                EmailAccount.status == "HEALTH_CRITICAL"
            )
        )
        return int(count or 0)

    def _disk_state(self) -> dict[str, Any]:
        try:
            usage = shutil.disk_usage(settings.ops_storage_path or ".")
            free_bytes = int(usage.free)
            total_bytes = int(usage.total) or 1
            free_fraction = free_bytes / total_bytes
        except OSError:
            return {"free_bytes": self.disk_min_free_bytes, "free_fraction": 1.0}
        return {"free_bytes": free_bytes, "free_fraction": free_fraction}

    def collect_state(self) -> dict[str, Any]:
        checks = readiness()
        failed_count, succeeded_count = self._provider_outcomes()
        disk = self._disk_state()
        return {
            "readiness": checks,
            "queue": {"delivery_backlog": self._queue_backlog()},
            "provider": {"failed": failed_count, "succeeded": succeeded_count},
            "webhooks": {"failed_verifications": self._webhook_failures()},
            "senders": {"critical": self._critical_senders()},
            "disk": disk,
            "thresholds": {
                "queue_growing_jobs": self.queue_growing_jobs,
                "provider_failure_rate": self.provider_failure_rate,
                "min_provider_attempts": self.min_provider_attempts,
                "disk_min_free_bytes": self.disk_min_free_bytes,
                "disk_min_free_fraction": self.disk_min_free_fraction,
                "critical_sender_limit": self.critical_sender_limit,
            },
        }

    # ------------------------------------------------------------------ #
    # Rules
    # ------------------------------------------------------------------ #
    def _rule_database_unavailable(self, state: dict[str, Any]) -> AlertResult | None:
        if state["readiness"].get("database") != "ready":
            return AlertResult(
                "database_unavailable",
                "Database unavailable",
                "critical",
                "The primary database failed its readiness probe.",
                {"status": state["readiness"].get("database")},
            )
        return None

    def _rule_redis_unavailable(self, state: dict[str, Any]) -> AlertResult | None:
        if state["readiness"].get("redis") != "ready":
            return AlertResult(
                "redis_unavailable",
                "Redis unavailable",
                "critical",
                "Redis failed its ping probe; rate limits and locks may degrade.",
                {"status": state["readiness"].get("redis")},
            )
        return None

    def _rule_worker_stopped(self, state: dict[str, Any]) -> AlertResult | None:
        if state["readiness"].get("worker") != "ready":
            return AlertResult(
                "worker_stopped",
                "Worker stopped",
                "critical",
                "Delivery worker heartbeat is stale or missing; jobs are not being processed.",
                {"status": state["readiness"].get("worker")},
            )
        return None

    def _rule_scheduler_stopped(self, state: dict[str, Any]) -> AlertResult | None:
        if state["readiness"].get("scheduler") != "ready":
            return AlertResult(
                "scheduler_stopped",
                "Scheduler stopped",
                "critical",
                "Beat scheduler heartbeat is stale or missing; due jobs are not dispatched.",
                {"status": state["readiness"].get("scheduler")},
            )
        return None

    def _rule_queue_growing(self, state: dict[str, Any]) -> AlertResult | None:
        backlog = int(state["queue"].get("delivery_backlog") or 0)
        threshold = int(state["thresholds"]["queue_growing_jobs"])
        if backlog > threshold:
            return AlertResult(
                "queue_growing",
                "Delivery queue growing",
                "warning",
                f"Delivery job backlog is {backlog} (threshold {threshold}); recovery may be delayed.",
                {"backlog": backlog, "threshold": threshold},
            )
        return None

    def _rule_high_provider_failure_rate(self, state: dict[str, Any]) -> AlertResult | None:
        failed = int(state["provider"].get("failed") or 0)
        succeeded = int(state["provider"].get("succeeded") or 0)
        attempts = failed + succeeded
        if attempts < int(state["thresholds"]["min_provider_attempts"]):
            return None
        rate = failed / attempts
        threshold = float(state["thresholds"]["provider_failure_rate"])
        if rate > threshold:
            return AlertResult(
                "high_provider_failure_rate",
                "High provider failure rate",
                "warning",
                f"Provider failure rate is {rate:.1%} ({failed} failed / {attempts} attempts) in the last 15 minutes.",
                {"failed": failed, "succeeded": succeeded, "attempts": attempts, "rate": round(rate, 6)},
            )
        return None

    def _rule_webhook_verification_failure(self, state: dict[str, Any]) -> AlertResult | None:
        failures = int(state["webhooks"].get("failed_verifications") or 0)
        if failures > 0:
            return AlertResult(
                "webhook_verification_failure",
                "Webhook verification failures",
                "warning",
                f"{failures} webhook(s) failed signature verification in the last 15 minutes; delivered events may not be recorded.",
                {"failed_verifications": failures},
            )
        return None

    def _rule_critical_sender_health(self, state: dict[str, Any]) -> AlertResult | None:
        critical = int(state["senders"].get("critical") or 0)
        if critical > 0:
            return AlertResult(
                "critical_sender_health",
                "Critical sender health",
                "warning",
                f"{critical} sender account(s) are in HEALTH_CRITICAL state and are blocked from sending.",
                {"critical_senders": critical},
            )
        return None

    def _rule_disk_storage(self, state: dict[str, Any]) -> AlertResult | None:
        free_bytes = int(state["disk"].get("free_bytes") or 0)
        free_fraction = float(state["disk"].get("free_fraction") or 0)
        low_bytes = free_bytes < int(state["thresholds"]["disk_min_free_bytes"])
        low_fraction = free_fraction < float(state["thresholds"]["disk_min_free_fraction"])
        if low_bytes or low_fraction:
            return AlertResult(
                "disk_storage",
                "Disk storage low",
                "critical",
                f"Only {free_bytes / 1024 / 1024:.1f} MB free on ops storage ({free_fraction:.1%}).",
                {"free_bytes": free_bytes, "free_fraction": round(free_fraction, 6)},
            )
        return None

    def rules(self) -> list[AlertRuleSpec]:
        return [
            AlertRuleSpec("database_unavailable", "Database unavailable", "critical", self._rule_database_unavailable),
            AlertRuleSpec("redis_unavailable", "Redis unavailable", "critical", self._rule_redis_unavailable),
            AlertRuleSpec("worker_stopped", "Worker stopped", "critical", self._rule_worker_stopped),
            AlertRuleSpec("scheduler_stopped", "Scheduler stopped", "critical", self._rule_scheduler_stopped),
            AlertRuleSpec("queue_growing", "Delivery queue growing", "warning", self._rule_queue_growing),
            AlertRuleSpec("high_provider_failure_rate", "High provider failure rate", "warning", self._rule_high_provider_failure_rate),
            AlertRuleSpec("webhook_verification_failure", "Webhook verification failures", "warning", self._rule_webhook_verification_failure),
            AlertRuleSpec("critical_sender_health", "Critical sender health", "warning", self._rule_critical_sender_health),
            AlertRuleSpec("disk_storage", "Disk storage low", "critical", self._rule_disk_storage),
        ]

    def evaluate(self, state: dict[str, Any] | None = None) -> list[AlertResult]:
        if state is None:
            state = self.collect_state()
        firing: list[AlertResult] = []
        for spec in self.rules():
            result = spec.evaluate(state)
            if result is not None:
                firing.append(result)
        return firing

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def reconcile(
        self,
        results: list[AlertResult],
        state: dict[str, Any] | None = None,
    ) -> dict[str, int]:
        """Open/update firing alerts; resolve ones that stopped firing.

        Returns counts of created/resolved alerts so the API can report them.
        """
        if state is None:
            state = self.collect_state()
        firing_rules = {result.rule for result in results}
        created = 0
        resolved = 0
        for result in results:
            record = self.session.scalar(
                select(AlertRecord)
                .where(
                    AlertRecord.rule == result.rule,
                    AlertRecord.status.in_(["OPEN", "ACKNOWLEDGED"]),
                )
                .order_by(AlertRecord.created_at.desc())
                .limit(1)
            )
            now = datetime.now(UTC)
            if record is None:
                record = AlertRecord(
                    rule=result.rule,
                    severity=result.severity,
                    metric=result.rule,
                    status="OPEN",
                    message=result.message,
                    details=result.details,
                )
                self.session.add(record)
                created += 1
            else:
                record.message = result.message
                record.details = result.details
                record.updated_at = now
        for record in self.session.scalars(
            select(AlertRecord).where(AlertRecord.status.in_(["OPEN", "ACKNOWLEDGED"]))
        ).all():
            if record.rule not in firing_rules:
                record.status = "RESOLVED"
                record.resolved_at = datetime.now(UTC)
                record.updated_at = datetime.now(UTC)
                resolved += 1
        self.session.commit()
        log_event("ops.alerts.reconciled", created=created, resolved=resolved)
        return {"created": created, "resolved": resolved}

    def open_alerts(self, limit: int = 50) -> list[AlertRecord]:
        return list(
            self.session.scalars(
                select(AlertRecord)
                .where(AlertRecord.status.in_(["OPEN"]))
                .order_by(AlertRecord.created_at.desc())
                .limit(limit)
            )
        )

    def recent(self, limit: int = 50) -> list[AlertRecord]:
        return list(
            self.session.scalars(
                select(AlertRecord)
                .order_by(AlertRecord.created_at.desc())
                .limit(limit)
            )
        )

    def ack(self, alert_id: UUID, actor_id: UUID) -> AlertRecord | None:
        record = self.session.get(AlertRecord, alert_id)
        if record is None or record.status == "RESOLVED":
            return None
        record.status = "ACKNOWLEDGED"
        record.acknowledged_at = datetime.now(UTC)
        record.acknowledged_by = actor_id
        record.updated_at = datetime.now(UTC)
        self.session.commit()
        return record


def serialize_alert(record: AlertRecord) -> dict[str, Any]:
    return {
        "id": str(record.id),
        "rule": record.rule,
        "severity": record.severity,
        "metric": record.metric,
        "status": record.status,
        "message": record.message,
        "details": record.details,
        "tenant_id": str(record.tenant_id) if record.tenant_id else None,
        "created_at": record.created_at.isoformat(),
        "acknowledged_at": record.acknowledged_at.isoformat() if record.acknowledged_at else None,
        "resolved_at": record.resolved_at.isoformat() if record.resolved_at else None,
    }