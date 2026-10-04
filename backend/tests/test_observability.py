from __future__ import annotations

import json
import logging
from typing import Any
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.logging import (
    RequestContext,
    StructuredFormatter,
    current_request_id,
    redact,
)
from app.models import Base, OpsMetricSample
from app.observability.alerts import AlertService, NoOpMetrics, serialize_alert
from app.observability.metrics import MetricsService
from app.observability.ops import OpsService


# --------------------------------------------------------------------------- #
# Fake Redis
# --------------------------------------------------------------------------- #
class FakeRedis:
    """Dict-backed redis client supporting the subset metrics/ops use."""

    def __init__(self) -> None:
        self._data: dict[str, dict[str, str]] = {}
        self._scalars: dict[str, str] = {}
        self._lists: dict[str, list[str]] = {}
        self._ttls: dict[str, int] = {}

    def _hash(self, key: str) -> dict[str, str]:
        return self._data.setdefault(key, {})

    def hset(self, key: str, field: str, value: str) -> int:
        existing = field in self._hash(key)
        self._data[key][field] = value
        return 0 if existing else 1

    def hget(self, key: str, field: str) -> str | None:
        return self._data.get(key, {}).get(field)

    def hgetall(self, key: str) -> dict[str, str]:
        return dict(self._data.get(key, {}))

    def hincrby(self, key: str, field: str, amount: int) -> int:
        current = int(self._data.get(key, {}).get(field, 0))
        new_value = current + int(amount)
        self._data.setdefault(key, {})[field] = str(new_value)
        return new_value

    def hincrbyfloat(self, key: str, field: str, amount: str | float) -> str:
        current = float(self._data.get(key, {}).get(field, 0))
        new_value = current + float(amount)
        self._data.setdefault(key, {})[field] = str(new_value)
        return str(new_value)

    def exists(self, key: str) -> int:
        return 1 if key in self._data or key in self._lists or key in self._scalars else 0

    def expire(self, key: str, ttl: int) -> bool:
        self._ttls[key] = int(ttl)
        return True

    def keys(self, pattern: str) -> list[str]:
        prefix = pattern.replace("*", "")
        return [key for key in {*self._data, *self._lists, *self._scalars} if key.startswith(prefix)]

    def get(self, key: str) -> str | None:
        return self._scalars.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> bool:
        self._scalars[key] = value
        if ex is not None:
            self._ttls[key] = ex
        return True

    def llen(self, key: str) -> int:
        return len(self._lists.get(key, []))

    def ping(self) -> bool:
        return True


@pytest.fixture()
def session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'obs.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    yield factory
    engine.dispose()


# --------------------------------------------------------------------------- #
# Structured logging
# --------------------------------------------------------------------------- #
def test_request_context_sets_request_id() -> None:
    assert current_request_id() == ""
    with RequestContext(request_id="req-123"):
        assert current_request_id() == "req-123"
    assert current_request_id() == ""


def test_structured_formatter_emits_json_with_context() -> None:
    formatter = StructuredFormatter()
    with RequestContext(request_id="abc-123", tenant_id="tenant-1", user_id="user-1"):
        record = logging.LogRecord(
            name="crcrm.test",
            level=logging.INFO,
            pathname=__file__,
            lineno=1,
            msg="hello %s",
            args=("world",),
            exc_info=None,
        )
        record.method = "GET"
        record.status = 200
        record.duration_ms = 12.5
        payload = json.loads(formatter.format(record))
    assert payload["level"] == "info"
    assert payload["message"] == "hello world"
    assert payload["request_id"] == "abc-123"
    assert payload["tenant_id"] == "tenant-1"
    assert payload["user_id"] == "user-1"
    assert payload["method"] == "GET"
    assert payload["status"] == 200
    assert payload["duration_ms"] == 12.5


def test_redact_hides_secrets_in_nested_dicts() -> None:
    value = {
        "name": "ok",
        "client_secret": "super-secret",
        "nested": {"api_key": "key-123", "smtp_password": "pw", "keep": "yes"},
    }
    result = redact(value)
    assert result["name"] == "ok"
    assert result["client_secret"] == "[REDACTED]"
    assert result["nested"]["api_key"] == "[REDACTED]"
    assert result["nested"]["smtp_password"] == "[REDACTED]"
    assert result["nested"]["keep"] == "yes"


# --------------------------------------------------------------------------- #
# Metrics service
# --------------------------------------------------------------------------- #
def test_metrics_counter_and_snapshot() -> None:
    redis = FakeRedis()
    service = MetricsService(redis, ttl_seconds=60)

    service.counter("api.requests", route="/api/v1/contacts", status_bucket="2xx")
    service.counter("api.requests", route="/api/v1/contacts", status_bucket="2xx")
    service.counter("api.requests", route="/api/v1/auth/*", status_bucket="4xx")

    snapshot = service.snapshot("api.requests")
    contacts = snapshot["route=/api/v1/contacts&status_bucket=2xx"]
    assert contacts["count"] == 2
    auth = snapshot["route=/api/v1/auth/*&status_bucket=4xx"]
    assert auth["count"] == 1


def test_metrics_gauge_and_latency() -> None:
    redis = FakeRedis()
    service = MetricsService(redis, ttl_seconds=60)

    service.gauge("queue.delivery.due", 42.0)
    assert float(service._redis.hget("crcrm:metrics:queue.delivery.due", "__default__") or 0) == 42.0

    service.record_latency("api.latency", 0.25, route="/api/v1/contacts")
    service.record_latency("api.latency", 0.75, route="/api/v1/contacts")

    bucket = service.snapshot("api.latency")["route=/api/v1/contacts"]
    assert bucket["count"] == 2
    assert bucket["avg"] == pytest.approx(0.5, abs=0.000001)
    assert bucket["max"] == 0.75
    assert bucket["sum"] == pytest.approx(1.0, abs=0.000001)


# --------------------------------------------------------------------------- #
# OpsService sampling
# --------------------------------------------------------------------------- #
def test_ops_sample_persists_rows_and_gauges(session_factory) -> None:
    redis = FakeRedis()
    session = session_factory()
    service = OpsService(session, MetricsService(redis, ttl_seconds=60), redis)

    values = service.sample()

    assert values["db.latency"] is not None
    assert values["queue.delivery"] is not None
    rows = session.query(OpsMetricSample).all()
    assert len(rows) >= 1
    assert {row.metric for row in rows} >= {"db.latency", "queue.delivery"}
    history = service.recent_samples(limit=10)
    assert isinstance(history[0]["metric"], str)
    assert "sampled_at" in history[0]
    session.close()


# --------------------------------------------------------------------------- #
# Alerts
# --------------------------------------------------------------------------- #
def _alert_state(**overrides: Any) -> dict[str, Any]:
    state: dict[str, Any] = {
        "readiness": {
            "database": "ready",
            "redis": "ready",
            "migrations": "ready",
            "worker": "ready",
            "scheduler": "ready",
        },
        "queue": {"delivery_backlog": 0},
        "provider": {"failed": 0, "succeeded": 0},
        "webhooks": {"failed_verifications": 0},
        "senders": {"critical": 0},
        "disk": {"free_bytes": 10 * 1024 * 1024 * 1024, "free_fraction": 0.9},
        "thresholds": {
            "queue_growing_jobs": 250,
            "provider_failure_rate": 0.10,
            "min_provider_attempts": 20,
            "disk_min_free_bytes": 500 * 1024 * 1024,
            "disk_min_free_fraction": 0.05,
            "critical_sender_limit": 1,
        },
    }
    for section in ("readiness", "queue", "provider", "webhooks", "senders", "disk", "thresholds"):
        if section in overrides:
            state[section].update(overrides[section])
    return state


def test_alert_evaluate_no_conditions(session_factory) -> None:
    service = AlertService(session_factory(), NoOpMetrics())
    assert service.evaluate(_alert_state()) == []


def test_alert_rules_fire(session_factory) -> None:
    service = AlertService(session_factory(), NoOpMetrics())

    cases = [
        (_alert_state(readiness={"database": "failed"}), "database_unavailable"),
        (_alert_state(readiness={"redis": "failed"}), "redis_unavailable"),
        (_alert_state(readiness={"worker": "failed"}), "worker_stopped"),
        (_alert_state(readiness={"scheduler": "failed"}), "scheduler_stopped"),
        (_alert_state(queue={"delivery_backlog": 500}), "queue_growing"),
        (_alert_state(provider={"failed": 18, "succeeded": 2}), "high_provider_failure_rate"),
        (_alert_state(webhooks={"failed_verifications": 3}), "webhook_verification_failure"),
        (_alert_state(senders={"critical": 2}), "critical_sender_health"),
        (_alert_state(disk={"free_bytes": 1000, "free_fraction": 0.001}), "disk_storage"),
    ]
    for state, rule in cases:
        fired = {result.rule for result in service.evaluate(state)}
        assert rule in fired, f"{rule} should fire for {state}"


def test_alert_reconcile_opens_updates_and_resolves(session_factory) -> None:
    session = session_factory()
    service = AlertService(session, NoOpMetrics())

    firing_state = _alert_state(webhooks={"failed_verifications": 1})
    results = service.evaluate(firing_state)
    counts = service.reconcile(results)
    assert counts["created"] == 1
    counts2 = service.reconcile(service.evaluate(firing_state))
    assert counts2["created"] == 0
    open_alerts = service.open_alerts()
    assert len(open_alerts) == 1
    assert open_alerts[0].rule == "webhook_verification_failure"
    assert open_alerts[0].status == "OPEN"

    resolved = service.reconcile(service.evaluate(_alert_state()))
    assert resolved["resolved"] == 1
    recent = service.recent()
    assert recent[0].status == "RESOLVED"
    assert recent[0].resolved_at is not None
    session.close()


def test_alert_ack(session_factory) -> None:
    session = session_factory()
    service = AlertService(session, NoOpMetrics())
    service.reconcile(service.evaluate(_alert_state(webhooks={"failed_verifications": 1})))
    record = service.open_alerts()[0]
    actor = uuid4()
    acknowledged = service.ack(record.id, actor)
    assert acknowledged is not None
    assert acknowledged.status == "ACKNOWLEDGED"
    assert acknowledged.acknowledged_by == actor
    assert service.open_alerts() == []
    assert service.recent()[0].status == "ACKNOWLEDGED"
    payload = serialize_alert(acknowledged)
    assert payload["status"] == "ACKNOWLEDGED"
    session.close()


def test_real_state_collection_runs_on_empty_db(session_factory) -> None:
    """collect_state works against a live (empty) DB without raising."""
    session = session_factory()
    service = AlertService(session, NoOpMetrics())
    state = service.collect_state()
    assert set(state) == {"readiness", "queue", "provider", "webhooks", "senders", "disk", "thresholds"}
    session.close()


def test_ops_sample_no_redis(session_factory) -> None:
    session = session_factory()
    service = OpsService(session, NoOpMetrics(), redis=None)
    values = service.sample()
    assert values["redis.latency"] is None
    assert values["queue.celery"] is None
    session.close()