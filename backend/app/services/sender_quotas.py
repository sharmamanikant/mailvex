"""System B per-sender daily quotas (Phase 10Q).

Both campaign and warmup sends are metered per sender against their configured
daily limits. Unlike the security rate limiter (which may fail open in dev),
Phase 10Q quotas **fail closed**: if Redis is unavailable the send is refused,
because an unmetered sender would otherwise blow past warmup/deliverability
guardrails. With ``daily_campaign_limit`` / ``daily_warmup_limit`` set to
``None`` a sender is uncapped and no Redis key is touched.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from app.models import SenderAccount
from app.security.rate_limit import RateLimitResult, RateLimitService

_DAILY_WINDOW_SECONDS = 86_400


class SenderQuotaExceeded(Exception):
    """Raised when a sender has exhausted its daily send budget."""

    def __init__(self, kind: str, limit: int, retry_after: int) -> None:
        self.kind = kind
        self.limit = limit
        self.retry_after = retry_after
        super().__init__(f"{kind} daily limit of {limit} reached; retry after {retry_after}s")


class SenderQuotaService:
    """Meters campaign and warmup sends per SenderAccount against Redis."""

    def __init__(self, rate_limits: RateLimitService) -> None:
        self.rate_limits = rate_limits

    def reserve_campaign(self, account: SenderAccount, limit: int | None = None) -> None:
        self._guarded(account, "campaign", limit if limit is not None else account.daily_campaign_limit)

    def reserve_warmup(self, account: SenderAccount) -> None:
        self._guarded(account, "warmup", account.daily_warmup_limit)

    def check_campaign(
        self, sender_id: UUID, limit: int | None
    ) -> RateLimitResult:
        return self._check(_key(sender_id, "campaign"), limit)

    def check_warmup(
        self, sender_id: UUID, limit: int | None
    ) -> RateLimitResult:
        return self._check(_key(sender_id, "warmup"), limit)

    # ------------------------------------------------------------------ #
    def _guarded(self, account: SenderAccount, kind: str, limit: int | None) -> None:
        result = self._check(_key(account.id, kind), limit)
        if not result.allowed:
            raise SenderQuotaExceeded(kind, limit or 0, result.retry_after)

    def _check(self, key: str, limit: int | None) -> RateLimitResult:
        if limit is None:
            return RateLimitResult(True, 0)
        return self.rate_limits.check_limit(key, max(1, limit), _DAILY_WINDOW_SECONDS)


def _key(sender_id: UUID, kind: str) -> str:
    today = datetime.now(UTC).strftime("%Y-%m-%d")
    return f"phase10q:{sender_id}:{kind}:{today}"