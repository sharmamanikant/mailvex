"""Operational sampling and dashboard overview assembly."""

from __future__ import annotations

import time
from datetime import UTC, datetime
from typing import Any, cast
from uuid import UUID

from redis import Redis
from sqlalchemy import select, text
from sqlalchemy.orm import Session

from app.core.logging import log_event
from app.models import DeliveryJob, OpsMetricSample, ScheduledMessage
from app.observability.alerts import AlertService, serialize_alert
from app.observability.metrics import MetricsService, NoOpMetrics
from app.services.readiness import readiness

BROKER_QUEUE_KEY = "celery"


class OpsService:
    """Samples system health and assembles the operations overview."""

    def __init__(
        self,
        session: Session,
        metrics: MetricsService | NoOpMetrics,
        redis: Redis | None = None,
    ) -> None:
        self.session = session
        self.metrics = metrics
        self.redis = redis

    # ------------------------------------------------------------------ #
    # Probes
    # ------------------------------------------------------------------ #
    def _db_latency_ms(self) -> float | None:
        start = time.perf_counter()
        try:
            self.session.execute(text("SELECT 1"))
        except Exception:
            return None
        return (time.perf_counter() - start) * 1000

    def _redis_probe(self) -> tuple[float | None, int | None]:
        """Returns (latency_ms, broker_depth)."""
        if self.redis is None:
            return (None, None)
        start = time.perf_counter()
        try:
            self.redis.ping()
            latency_ms = (time.perf_counter() - start) * 1000
        except Exception:
            return (None, None)
        try:
            broker_depth = int(cast(Any, self.redis.llen(BROKER_QUEUE_KEY)) or 0)
        except Exception:
            broker_depth = None
        return (latency_ms, broker_depth)

    def _scheduled_backlog(self) -> int:
        from sqlalchemy import func

        now = datetime.now(UTC)
        total = self.session.scalar(
            select(func.count()).select_from(ScheduledMessage).where(
                ScheduledMessage.status.in_(["QUEUED", "DEFERRED"]),
                ScheduledMessage.due_at <= now,
            )
        )
        return int(total or 0)

    def _delivery_backlog(self) -> int:
        from sqlalchemy import func

        now = datetime.now(UTC)
        total = self.session.scalar(
            select(func.count()).select_from(DeliveryJob).where(
                DeliveryJob.status.in_(["PENDING", "PROCESSING"]),
                (DeliveryJob.next_attempt_at.is_(None)) | (DeliveryJob.next_attempt_at <= now),
            )
        )
        return int(total or 0)

    def _heartbeat_age(self, name: str) -> float | None:
        if self.redis is None:
            return None
        try:
            raw = self.redis.get(f"crcrm:heartbeat:{name}")
        except Exception:
            return None
        if not raw:
            return None
        try:
            value = datetime.fromisoformat(str(raw).replace("Z", "+00:00"))
            if value.tzinfo is None:
                value = value.replace(tzinfo=UTC)
        except ValueError:
            return None
        return (datetime.now(UTC) - value).total_seconds()

    # ------------------------------------------------------------------ #
    # Sampling
    # ------------------------------------------------------------------ #
    def sample(self, tenant_id: UUID | None = None) -> dict[str, float | None]:
        """Measure current system state, persist samples, publish gauges.

        Runs inside the calling transaction: rows are committed by the caller
        (scheduler task or API endpoint). Returns the sampled values.
        """
        db_latency_ms = self._db_latency_ms()
        redis_latency_ms, broker_depth = self._redis_probe()
        scheduled_backlog = self._scheduled_backlog()
        delivery_backlog = self._delivery_backlog()
        worker_age = self._heartbeat_age("worker")
        scheduler_age = self._heartbeat_age("scheduler")

        values: dict[str, float | None] = {
            "db.latency": db_latency_ms,
            "redis.latency": redis_latency_ms,
            "queue.celery": broker_depth,
            "queue.scheduled": scheduled_backlog,
            "queue.delivery": delivery_backlog,
            "heartbeat.worker": worker_age,
            "heartbeat.scheduler": scheduler_age,
        }
        for name, value in values.items():
            if value is not None:
                self.metrics.gauge(f"ops.{name}", float(value))
                self.session.add(
                    OpsMetricSample(
                        tenant_id=tenant_id,
                        metric=name,
                        value=float(value),
                        labels={},
                    )
                )
        self.session.commit()
        log_event("ops.sample", **{name: value for name, value in values.items() if value is not None})
        return values

    # ------------------------------------------------------------------ #
    # Overview
    # ------------------------------------------------------------------ #
    def overview(self) -> dict[str, Any]:
        checks = readiness()
        metrics = self.metrics.snapshot_all() if hasattr(self.metrics, "snapshot_all") else {}
        recent_samples = self.recent_samples(limit=50)
        alert_service = AlertService(self.session, self.metrics)
        open_alerts = [
            serialize_alert(record)
            for record in alert_service.open_alerts(limit=25)
        ]
        latest: dict[str, float | None] = {}
        for sample in recent_samples:
            if sample["metric"] not in latest:
                latest[sample["metric"]] = sample["value"]
        return {
            "readiness": checks,
            "ready": all(value == "ready" for value in checks.values()),
            "metrics": metrics,
            "samples": recent_samples,
            "latest_samples": latest,
            "alerts": {"open": open_alerts, "count": len(open_alerts)},
            "sampled_at": datetime.now(UTC).isoformat(),
        }

    def recent_samples(self, limit: int = 50) -> list[dict[str, Any]]:
        rows = self.session.scalars(
            select(OpsMetricSample)
            .order_by(OpsMetricSample.sampled_at.desc())
            .limit(limit)
        ).all()
        return [
            {
                "id": str(row.id),
                "metric": row.metric,
                "value": row.value,
                "labels": row.labels,
                "tenant_id": str(row.tenant_id) if row.tenant_id else None,
                "sampled_at": row.sampled_at.isoformat(),
            }
            for row in rows
        ]

    def metrics_history(
        self,
        metric: str | None = None,
        limit: int = 100,
    ) -> list[dict[str, Any]]:
        query = select(OpsMetricSample)
        if metric:
            query = query.where(OpsMetricSample.metric == metric)
        rows = self.session.scalars(query.order_by(OpsMetricSample.sampled_at.desc()).limit(limit)).all()
        return [
            {
                "id": str(row.id),
                "metric": row.metric,
                "value": row.value,
                "labels": row.labels,
                "sampled_at": row.sampled_at.isoformat(),
            }
            for row in rows
        ]