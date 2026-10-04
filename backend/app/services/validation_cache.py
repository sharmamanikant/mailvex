"""Redis cache for the contact validation engine.

Cache design notes
-----------------
* **Domain keys are global.** ``domain_status`` / ``mx_status`` / ``catch_all``
  are public facts about public DNS, identical for every tenant, so they are
  shared. Re-resolving ``gmail.com`` once per tenant would be wasteful.
* **Address keys are tenant-scoped.** Email and phone results can be tied to a
  specific contact, so they live under a tenant namespace to keep one tenant's
  cached verdicts unreachable from another.
* **Redis is optional at runtime.** Every helper degrades to a no-op when Redis
  is unreachable; validation still runs, it just re-resolves DNS. A cache
  outage must never be able to block a verification run.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
from typing import Any

from redis import Redis
from redis.exceptions import RedisError

logger = logging.getLogger(__name__)

_DOMAIN_PREFIX = "crcrm:val:domain"
_EMAIL_PREFIX = "crcrm:val:email"
_PHONE_PREFIX = "crcrm:val:phone"
_CIRCUIT_PREFIX = "crcrm:val:smtp-circuit"


def _client() -> Redis:
    return Redis.from_url(
        os.getenv("REDIS_URL", "redis://localhost:6379/0"),
        decode_responses=True,
        socket_connect_timeout=2.0,
        socket_timeout=2.0,
    )


def _fingerprint(value: str) -> str:
    return hashlib.sha256(value.strip().lower().encode("utf-8")).hexdigest()[:40]


def get_json(key: str) -> dict[str, Any] | None:
    try:
        raw = _client().get(key)
    except RedisError as exc:
        logger.warning("validation cache read failed for %s: %s", key, exc)
        return None
    if not raw:
        return None
    if not isinstance(raw, (str, bytes, bytearray)):
        return None
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError:
        return None
    return parsed if isinstance(parsed, dict) else None


def set_json(key: str, value: dict[str, Any], ttl_seconds: int) -> None:
    try:
        _client().set(key, json.dumps(value, default=str), ex=ttl_seconds)
    except RedisError as exc:
        logger.warning("validation cache write failed for %s: %s", key, exc)


def delete(key: str) -> None:
    try:
        _client().delete(key)
    except RedisError as exc:
        logger.warning("validation cache delete failed for %s: %s", key, exc)


def domain_key(domain: str) -> str:
    return f"{_DOMAIN_PREFIX}:{_fingerprint(domain)}"


def email_key(tenant_id: str, email: str) -> str:
    return f"{_EMAIL_PREFIX}:{tenant_id}:{_fingerprint(email)}"


def phone_key(tenant_id: str, phone: str) -> str:
    return f"{_PHONE_PREFIX}:{tenant_id}:{_fingerprint(phone)}"


def circuit_key(host: str) -> str:
    return f"{_CIRCUIT_PREFIX}:{host}"


def circuit_open(host: str) -> bool:
    """True when the host is currently circuit-broken (probes should be skipped)."""
    try:
        return bool(_client().exists(circuit_key(host)))
    except RedisError:
        return False


def record_smtp_outcome(host: str, failures: int, threshold: int, cooldown_seconds: int) -> None:
    """Open the circuit for ``host`` once consecutive failures reach ``threshold``."""
    if failures < threshold:
        return
    try:
        _client().set(circuit_key(host), "open", ex=cooldown_seconds)
    except RedisError as exc:
        logger.warning("validation circuit write failed for %s: %s", host, exc)


def clear_circuit(host: str) -> None:
    delete(circuit_key(host))
