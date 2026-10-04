"""OAuth state store for sender-provider consent flows (Phase 10B).

State is opaque, cryptographically-random, bound to the initiating tenant and
user, single-use, and expires after :data:`OAuthStateStore.TTL_SECONDS`.
Production uses Redis with an atomic ``GETDEL`` so a replayed callback can
never redeem the same state twice. Tests inject an in-memory fallback; when
Redis is unavailable outside tests the store fails closed (it raises rather
than weakening state security).
"""

from __future__ import annotations

import hashlib
import json
import secrets
import threading
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from redis import Redis

from app.core.config import settings
from app.observability.metrics import get_redis

__all__ = ["OAuthStateError", "OAuthStateRecord", "OAuthStateStore"]


class OAuthStateError(ValueError):
    pass


@dataclass(frozen=True)
class OAuthStateRecord:
    tenant_id: UUID
    user_id: UUID
    connection_id: UUID | None
    created_at: datetime
    expires_at: datetime


class OAuthStateStore:
    TTL_SECONDS = 600  # google_auth_oauthlib consent windows are minutes.

    _PREFIX = "crcrm:oauth:state"

    def __init__(self, redis: Redis | None = None, *, memory_fallback: bool | None = None) -> None:
        use_memory = memory_fallback if memory_fallback is not None else settings.app_env == "test"
        if use_memory:
            self._redis: Redis | None = None
            self._memory: dict[str, OAuthStateRecord] | None = {}
        else:
            self._redis = redis
            self._memory = None
        self._lock = threading.RLock()

    # ------------------------------------------------------------------ #
    # Issuance / consumption
    # ------------------------------------------------------------------ #
    def create(self, *, tenant_id: UUID, user_id: UUID, connection_id: UUID | None = None) -> str:
        state = secrets.token_urlsafe(32)
        now = datetime.now(UTC)
        record = OAuthStateRecord(
            tenant_id=tenant_id,
            user_id=user_id,
            connection_id=connection_id,
            created_at=now,
            expires_at=now + timedelta(seconds=self.TTL_SECONDS),
        )
        key = self._key(state)
        if self._redis is not None:
            self._redis.set(key, json.dumps(self._encode(record)), ex=self.TTL_SECONDS)
        else:
            memory = self._memory
            if memory is None:
                raise OAuthStateError("OAuth state store is unavailable")
            with self._lock:
                memory[key] = record
        return state

    def consume(self, state: str) -> OAuthStateRecord:
        """Atomically redeem (and delete) a state. Reuse/forgery is rejected."""
        key = self._key(state)
        if self._redis is not None:
            raw = cast(Any, self._redis.getdel(key))
            if not raw:
                raise OAuthStateError("Invalid or already-used OAuth state")
            try:
                record = self._decode(raw)
            except (KeyError, TypeError, ValueError) as exc:
                raise OAuthStateError("Invalid OAuth state") from exc
        else:
            memory = self._memory
            if memory is None:
                raise OAuthStateError("OAuth state store is unavailable")
            with self._lock:
                try:
                    record = memory.pop(key)
                except KeyError as exc:
                    raise OAuthStateError("Invalid or already-used OAuth state") from exc
        if record.expires_at <= datetime.now(UTC):
            raise OAuthStateError("OAuth state has expired; please start again")
        return record

    # ------------------------------------------------------------------ #
    # Plumbing
    # ------------------------------------------------------------------ #
    @classmethod
    def _key(cls, state: str) -> str:
        digest = hashlib.sha256(state.encode("utf-8")).hexdigest()
        return f"{cls._PREFIX}:{digest}"

    @staticmethod
    def _encode(record: OAuthStateRecord) -> dict[str, Any]:
        return {
            "tenant_id": str(record.tenant_id),
            "user_id": str(record.user_id),
            "connection_id": str(record.connection_id) if record.connection_id else None,
            "created_at": record.created_at.isoformat(),
            "expires_at": record.expires_at.isoformat(),
        }

    @staticmethod
    def _decode(raw: str | bytes) -> OAuthStateRecord:
        payload = json.loads(raw)
        if not isinstance(payload, dict):
            raise TypeError("Invalid state payload")
        return OAuthStateRecord(
            tenant_id=UUID(str(payload["tenant_id"])),
            user_id=UUID(str(payload["user_id"])),
            connection_id=UUID(str(payload["connection_id"])) if payload.get("connection_id") else None,
            created_at=datetime.fromisoformat(str(payload["created_at"])),
            expires_at=datetime.fromisoformat(str(payload["expires_at"])),
        )


def build_state_store() -> OAuthStateStore:
    """Default production store: Redis (fail closed)."""
    return OAuthStateStore(redis=get_redis())