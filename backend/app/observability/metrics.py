"""Compact, fail-open runtime metrics.

Counters, gauges, and latency buckets are stored in Redis hashes under
``crcrm:metrics:{name}`` (label buckets as fields). Every operation is
best-effort: if Redis is unreachable the service short-circuits for a few
seconds and swallows the error so metering can never break a request.
"""

from __future__ import annotations

import threading
import time
from typing import Any, cast

from redis import Redis

from app.core.config import settings
from app.core.redis_client import build_redis_client

_KEY_PREFIX = "crcrm:metrics"
_MAX_SUFFIX = ".max"


def _label_key(labels: dict[str, Any]) -> str:
    if not labels:
        return "__default__"
    return "&".join(f"{key}={value}" for key, value in sorted(labels.items()))


class NoOpMetrics:
    """Injectable no-op metrics used where metering is optional."""

    def counter(self, name: str, *, amount: int = 1, **labels: Any) -> None:
        ...

    def gauge(self, name: str, value: float, **labels: Any) -> None:
        ...

    def record_latency(self, name: str, seconds: float, **labels: Any) -> None:
        ...

    def snapshot(self, name: str) -> dict[str, dict[str, float | int | str]]:
        return {}

    def snapshot_all(self) -> dict[str, dict[str, dict[str, float | int | str]]]:
        return {}


class MetricsService:
    """Redis-backed counters / gauges / latency buckets (fail-open)."""

    _BROKEN_WINDOW = 5.0

    def __init__(self, redis: Redis, ttl_seconds: int = settings.ops_metrics_ttl_seconds) -> None:
        self._redis = redis
        self._ttl = int(ttl_seconds)
        self._broken_until = 0.0
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Fail-open guard
    # ------------------------------------------------------------------ #
    def _guard(fn: Any) -> Any:
        def wrapper(self: MetricsService, *args: Any, **kwargs: Any) -> Any:
            if time.monotonic() < self._broken_until:
                return None
            try:
                return fn(self, *args, **kwargs)
            except Exception:
                with self._lock:
                    self._broken_until = time.monotonic() + self._BROKEN_WINDOW
                return None

        return wrapper

    # ------------------------------------------------------------------ #
    # Recording
    # ------------------------------------------------------------------ #
    @_guard
    def counter(self, name: str, *, amount: int = 1, **labels: Any) -> None:
        key = f"{_KEY_PREFIX}:{name}"
        field = _label_key(labels)
        self._redis.hincrby(key, f"{field}.count", int(amount))
        self._redis.expire(key, self._ttl)

    @_guard
    def gauge(self, name: str, value: float, **labels: Any) -> None:
        key = f"{_KEY_PREFIX}:{name}"
        self._redis.hset(key, _label_key(labels), str(float(value)))
        self._redis.expire(key, self._ttl)

    @_guard
    def record_latency(self, name: str, seconds: float, **labels: Any) -> None:
        key = f"{_KEY_PREFIX}:{name}"
        field = _label_key(labels)
        self._redis.hincrbyfloat(key, f"{field}.sum", float(seconds))
        self._redis.hincrby(key, f"{field}.count", 1)
        max_key = f"{_KEY_PREFIX}:{name}{_MAX_SUFFIX}"
        current = self._redis.hget(max_key, field)
        try:
            current_value = float(current) if current is not None else None  # type: ignore[arg-type]
        except (TypeError, ValueError):
            current_value = None
        if current_value is None or seconds > current_value:
            self._redis.hset(max_key, field, str(float(seconds)))
        self._redis.expire(key, self._ttl)
        self._redis.expire(max_key, self._ttl)

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    def snapshot(self, name: str) -> dict[str, dict[str, float | int | str]]:
        if self._broken():
            return {}
        try:
            return self._snapshot_impl(name)
        except Exception:
            with self._lock:
                self._broken_until = time.monotonic() + self._BROKEN_WINDOW
            return {}

    def _snapshot_impl(self, name: str) -> dict[str, dict[str, float | int | str]]:
        key = f"{_KEY_PREFIX}:{name}"
        if not self._redis.exists(key):
            return {}
        fields = cast(dict[Any, Any], self._redis.hgetall(key))
        max_key = f"{_KEY_PREFIX}:{name}{_MAX_SUFFIX}"
        maxes = cast(dict[Any, Any], self._redis.hgetall(max_key)) if self._redis.exists(max_key) else {}
        grouped: dict[str, dict[str, Any]] = {}
        for field, raw_value in fields.items():
            if isinstance(field, bytes):
                field = field.decode("utf-8", "replace")
            if isinstance(raw_value, bytes):
                raw_value = raw_value.decode("utf-8", "replace")
            if field.endswith(".count"):
                label = field[: -len(".count")]
                grouped.setdefault(label, {})["count"] = int(raw_value or 0)
            elif field.endswith(".sum"):
                label = field[: -len(".sum")]
                grouped.setdefault(label, {})["sum"] = float(raw_value or 0)
            else:
                grouped.setdefault(field, {})["value"] = float(raw_value or 0)
        for label, data in grouped.items():
            count = data.get("count")
            if count is not None:
                total = data.get("sum")
                if total is not None:
                    data["avg"] = round(float(total) / int(count), 6)
            max_value = maxes.get(label)
            if max_value is not None:
                if isinstance(max_value, bytes):
                    max_value = max_value.decode("utf-8", "replace")
                try:
                    data["max"] = float(max_value)
                except (TypeError, ValueError):
                    data["max"] = None
            if data.get("count") is not None and data.get("sum") is None:
                data["sum"] = None
            if data.get("count") is None and data.get("value") is None:
                data["count"] = 0
        return {label: data for label, data in grouped.items()}

    def snapshot_all(self) -> dict[str, dict[str, dict[str, float | int | str]]]:
        if self._broken():
            return {}
        try:
            return self._snapshot_all_impl()
        except Exception:
            with self._lock:
                self._broken_until = time.monotonic() + self._BROKEN_WINDOW
            return {}

    def _broken(self) -> bool:
        return time.monotonic() < self._broken_until

    def _snapshot_all_impl(self) -> dict[str, dict[str, dict[str, float | int | str]]]:
        keys = list(cast(list[Any], self._redis.keys(f"{_KEY_PREFIX}:*")))
        output: dict[str, dict[str, dict[str, float | int | str]]] = {}
        for key in keys:
            if isinstance(key, bytes):
                key = key.decode("utf-8", "replace")
            if key.endswith(_MAX_SUFFIX):
                continue
            name = key[len(f"{_KEY_PREFIX}:") :]
            output[name] = self._snapshot_impl(name)
        return output


_redis_client: Redis | None = None
_metrics: MetricsService | None = None
_lock = threading.RLock()


def get_redis() -> Redis:
    """Shared application Redis client (lazy, fail-open callers).

    Timeouts are bounded so metering/sampling can never stall a request when
    Redis is unreachable (e.g. a silently-dropped connection).
    """
    global _redis_client
    if _redis_client is None:
        with _lock:
            if _redis_client is None:
                _redis_client = _build_client()
    return _redis_client


def _build_client() -> Redis:
    return build_redis_client(settings.redis_url)


def get_metrics() -> MetricsService:
    """Shared application metrics service (fail-open, never breaks callers)."""
    global _metrics
    if _metrics is None:
        with _lock:
            if _metrics is None:
                _metrics = MetricsService(get_redis())
    return _metrics