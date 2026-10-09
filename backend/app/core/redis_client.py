"""Single Redis client factory with fail-fast timeouts.

Every Redis call in the application used to build its own client.  Those
clients were inconsistent: the rate limiter (which sits on the request hot
path) connected with no ``socket_connect_timeout`` at all, so a Redis outage
blocked every in-flight request indefinitely instead of failing fast.  A
handful of other call sites let the resulting ``redis.exceptions.*`` escape into
request handlers and turned an infrastructure fault into a 500.

This module centralises construction so every client shares the same bounded
timeouts, and offers two error-handling helpers that map the two distinct
behaviours the application needs:

``safe_redis_call``
    For advisory or diagnostic work (heartbeats, caches, metrics) where losing
    the Redis round-trip must never abort the caller's real work.

``require_redis_call``
    For work that genuinely cannot proceed without the broker (handing a job to
    the queue).  Raises :class:`RedisUnavailable`, which the API layer turns
    into ``503`` so callers get a fast, honest answer instead of a hang.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Callable

from redis import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

DEFAULT_REDIS_URL = "redis://localhost:6379/0"

# Bounded on purpose.  These values govern how long a request thread can be
# parked when Redis is unhealthy; they must stay in the seconds range so a
# broker outage degrades into 5xx/503 rather than an exhausted worker pool.
SOCKET_CONNECT_TIMEOUT = float(os.getenv("REDIS_SOCKET_CONNECT_TIMEOUT", "2.0"))
SOCKET_TIMEOUT = float(os.getenv("REDIS_SOCKET_TIMEOUT", "2.0"))


class RedisUnavailable(RuntimeError):
    """A required Redis operation failed because Redis was unreachable."""


def redis_url() -> str:
    return os.getenv("REDIS_URL", DEFAULT_REDIS_URL)


def build_redis_client(url: str | None = None) -> Redis:
    """Return a Redis client whose connect and read paths are time-bounded."""
    return Redis.from_url(
        url or redis_url(),
        decode_responses=True,
        socket_connect_timeout=SOCKET_CONNECT_TIMEOUT,
        socket_timeout=SOCKET_TIMEOUT,
        retry_on_timeout=False,
    )


def safe_redis_call[T](
    operation: str,
    call: Callable[[], T],
    *,
    default: T | None = None,
) -> T | None:
    """Run an advisory Redis operation, degrading to ``default`` on failure.

    Use this for heartbeats, caches and metrics: the caller's real work must
    continue when Redis is down, so a failure here is logged and swallowed.
    """
    try:
        return call()
    except RedisError as exc:
        logger.warning("redis %s failed: %s", operation, exc)
        return default


def require_redis_call[T](operation: str, call: Callable[[], T]) -> T:
    """Run a Redis operation that cannot be skipped, failing fast when it errors.

    Raises :class:`RedisUnavailable` rather than letting the driver exception
    propagate, so callers can map an infrastructure outage onto a 503 without
    importing ``redis`` internals.
    """
    try:
        return call()
    except RedisError as exc:
        logger.error("redis %s unavailable: %s", operation, exc)
        raise RedisUnavailable(f"{operation} is temporarily unavailable") from exc
