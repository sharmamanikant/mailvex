from __future__ import annotations

from dataclasses import dataclass
from typing import Any, cast

from redis.exceptions import RedisError

from app.core.redis_client import build_redis_client


@dataclass(frozen=True)
class RateLimitResult:
    allowed: bool
    retry_after: int
    #: True when the decision was not an actual limit decision.  The quota
    #: services must be able to tell "the sender is capped" apart from "the
    #: counter store is down", because collapsing the two reports an
    #: infrastructure outage as a 24-hour quota exhaustion.
    unavailable: bool = False


class RateLimitService:
    # Increment and set the expiry in one Redis operation.  EXPIRE must only
    # happen for a new key; renewing it on every request turns a fixed window
    # into an accidental permanent lockout under sustained traffic.
    _CHECK_LIMIT_SCRIPT = """
    local count = redis.call('INCR', KEYS[1])
    if count == 1 then
        redis.call('EXPIRE', KEYS[1], ARGV[1])
    end
    local ttl = redis.call('TTL', KEYS[1])
    return {count, ttl}
    """

    def __init__(self, redis_url: str, fail_open: bool = False) -> None:
        # This client is used by the request middleware, so its connect and read
        # paths must be time-bounded: an unbounded connect turns a Redis outage
        # into a full outage of the API.
        self.redis = build_redis_client(redis_url)
        self.fail_open = fail_open

    def check_limit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        try:
            count, ttl = cast(
                tuple[int, int],
                self.redis.eval(self._CHECK_LIMIT_SCRIPT, 1, key, str(window_seconds)),
            )
            if int(cast(Any, count)) > limit:
                return RateLimitResult(False, max(1, int(ttl)))
            return RateLimitResult(True, 0)
        except RedisError:
            if self.fail_open:
                return RateLimitResult(True, 0)
            return RateLimitResult(False, window_seconds, unavailable=True)
