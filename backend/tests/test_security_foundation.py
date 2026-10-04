from __future__ import annotations

from uuid import uuid4

from redis.exceptions import ConnectionError as RedisConnectionError

from app.security.rate_limit import RateLimitService
from app.services.audit import _safe_metadata


class _RedisCounter:
    def __init__(self) -> None:
        self.count = 0
        self.ttl = -1

    def eval(self, _script: str, _keys: int, _key: str, window: int) -> list[int]:
        self.count += 1
        if self.count == 1:
            self.ttl = window
        return [self.count, self.ttl]


class _UnavailableRedis:
    def eval(self, *_args: object) -> None:
        raise RedisConnectionError("unavailable")


def _service(redis: object, fail_open: bool = False) -> RateLimitService:
    service = RateLimitService.__new__(RateLimitService)
    service.redis = redis  # type: ignore[assignment]
    service.fail_open = fail_open
    return service


def test_rate_limit_keeps_the_initial_window_and_returns_retry_after() -> None:
    service = _service(_RedisCounter())
    assert service.check_limit("key", 2, 60).allowed
    assert service.check_limit("key", 2, 60).allowed
    denied = service.check_limit("key", 2, 60)
    assert not denied.allowed
    assert denied.retry_after == 60


def test_rate_limit_fails_closed_when_redis_is_unavailable() -> None:
    result = _service(_UnavailableRedis()).check_limit("key", 1, 30)
    assert not result.allowed
    assert result.retry_after == 30


def test_rate_limit_can_fail_open_for_non_security_development_routes() -> None:
    assert _service(_UnavailableRedis(), fail_open=True).check_limit("key", 1, 30).allowed


def test_audit_metadata_redacts_credentials_recursively() -> None:
    safe = _safe_metadata({"password": "secret", "nested": {"api_key": "value"}, "ids": [str(uuid4())]})
    assert safe["password"] == "[REDACTED]"
    assert safe["nested"]["api_key"] == "[REDACTED]"
    assert len(safe["ids"]) == 1
