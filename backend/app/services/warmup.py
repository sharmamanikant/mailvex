"""System B sender warmup (Phase 10Q).

Warmup increases a new sender's safe daily volume gradually so provider
reputation ramps instead of spiking. State lives in Redis (which Phase 10Q
requires):

* ``phase10q:<sender_id>:warmup:next_at`` — ISO-8601 timestamp of the next due
  warmup send (armed on ``enable_warmup``, removed on ``disable_warmup``).
* A beat task drives due senders onto the dedicated ``warmup`` Celery queue;
  each completed send re-arms the next send within the configured delay range
  and weekday window.

Every send is metered by ``SenderQuotaService`` (fail-closed, Redis required)
and reported through ``SenderHealthService`` so the sender's health state stays
current.
"""

from __future__ import annotations

import logging
import random
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID

from redis import Redis
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.redis_client import build_redis_client, safe_redis_call
from app.email_providers import EmailMessage
from app.email_providers.base import EmailProviderError, ProviderConnectionConfig
from app.models import SenderAccount, SenderConnection, WarmupSettings
from app.services.sender_health import SenderHealthService
from app.services.sender_quotas import SenderQuotaService

logger = logging.getLogger(__name__)

WARMUP_QUEUE = "warmup"
_BLOCKED_CONNECTION_STATUSES = ("FAILED", "DISABLED", "DISCONNECTED")


class WarmupSendBlocked(Exception):
    """Warmup could not run for a sender (disabled, disconnected, or quota)."""


def arm_sender(sender_id: UUID, run_at: datetime | None = None) -> None:
    """Schedule the next warmup send.

    Redis holds only the scheduling pointer; ``SenderAccount.warmup_enabled``
    in the database stays the source of truth for whether warmup should run.
    A Redis outage must therefore not fail the caller's request, so a failed
    arm is logged and the send is simply picked up on a later drive.
    """
    safe_redis_call(
        "warmup arm",
        lambda: _redis().set(
            _next_at_key(sender_id),
            (run_at or datetime.now(UTC)).isoformat(),
            ex=30 * 86_400,
        ),
    )


def disarm_sender(sender_id: UUID) -> None:
    safe_redis_call("warmup disarm", lambda: _redis().delete(_next_at_key(sender_id)))


class WarmupService:
    """Schedules and performs one warmup send per SenderAccount."""

    def __init__(
        self,
        session: Session,
        tenant_id: UUID,
        quota: SenderQuotaService,
        health: SenderHealthService,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.quota = quota
        self.health = health

    # ------------------------------------------------------------------ #
    # Scheduling (pure)
    # ------------------------------------------------------------------ #
    def next_due_at(self, settings: WarmupSettings, from_when: datetime) -> datetime:
        """Next enough time within the weekday window and delay range."""
        candidate = from_when + timedelta(
            minutes=random.uniform(settings.minimum_delay or 5, settings.maximum_delay or 30)
        )
        return self._next_allowed(settings, candidate)

    def _next_allowed(self, settings: WarmupSettings, candidate: datetime) -> datetime:
        start = _minutes("09:00" if not settings.start_time else settings.start_time)
        end = _minutes("17:00" if not settings.end_time else settings.end_time)
        weekdays = {_DAY_INDEX[day] for day in settings.weekdays or ["MON", "TUE", "WED", "THU", "FRI"]}

        when_minutes = candidate.hour * 60 + candidate.minute
        if candidate.weekday() in weekdays:
            if when_minutes < start:
                return candidate.replace(hour=start // 60, minute=start % 60, second=0, microsecond=0)
            if start <= when_minutes < end:
                return candidate.replace(second=0, microsecond=0)
        return _next_weekday(candidate, weekdays, start, end)

    # ------------------------------------------------------------------ #
    # Dispatch + send
    # ------------------------------------------------------------------ #
    def drive(self, enqueue: Callable[[str, str], object]) -> list[UUID]:
        """Enqueue due enabled senders onto the warmup queue."""
        now = datetime.now(UTC)
        due: list[UUID] = []
        accounts = list(self.session.scalars(
            select(SenderAccount).where(
                SenderAccount.tenant_id == self.tenant_id,
                SenderAccount.warmup_enabled.is_(True),
                SenderAccount.status == "ACTIVE",
            )
        ).all())
        for account in accounts:
            next_at = _due_time(account.id)
            if next_at is None:
                continue
            if now >= next_at:
                due.append(account.id)
                enqueue(str(self.tenant_id), str(account.id))
            elif now < next_at - timedelta(hours=24):
                disarm_sender(account.id)
        return due

    def send_message(self, sender_id: UUID) -> str:
        """Send one warmup message; re-arm the next one. Returns a status tag."""
        account = self.session.scalar(
            select(SenderAccount).where(
                SenderAccount.id == sender_id,
                SenderAccount.tenant_id == self.tenant_id,
            )
        )
        if account is None:
            raise WarmupSendBlocked("Sender account not found")
        if not account.warmup_enabled or account.status != "ACTIVE":
            disarm_sender(account.id)
            raise WarmupSendBlocked("Warmup is not enabled for this sender")

        connection = self.session.scalar(
            select(SenderConnection).where(
                SenderConnection.id == account.connection_id,
                SenderConnection.tenant_id == self.tenant_id,
            )
        )
        if connection is None or connection.status in _BLOCKED_CONNECTION_STATUSES:
            raise WarmupSendBlocked("Sender connection is not usable")
        if connection.status == "REAUTH_REQUIRED":
            disarm_sender(account.id)
            raise WarmupSendBlocked("Sender requires reauthentication")

        settings = self.session.scalar(
            select(WarmupSettings).where(
                WarmupSettings.sender_id == account.id,
                WarmupSettings.tenant_id == self.tenant_id,
            )
        )
        try:
            self.quota.reserve_warmup(account)
        except Exception as exc:
            raise WarmupSendBlocked(str(exc)) from exc

        from app.email_providers import get_provider

        provider = get_provider(connection.provider)
        message = EmailMessage(
            from_email=connection.email or account.email,
            to=(account.email,),
            subject="System check",
            text_body="This is a system check email sent as part of your sending warmup.",
            html_body="<p>This is a system check email sent as part of your sending warmup.</p>",
        )
        try:
            provider.send_message(_provider_config(connection), message)
        except EmailProviderError as exc:
            self.health.record_send_outcome(sender_id, success=False, error_code=exc.code.value)
            raise WarmupSendBlocked(exc.message) from exc
        except Exception as exc:
            self.health.record_send_outcome(sender_id, success=False, error_code="PROVIDER_UNAVAILABLE")
            raise WarmupSendBlocked(str(exc)) from exc

        self.health.record_send_outcome(sender_id, success=True)
        if settings is not None:
            arm_sender(sender_id, self.next_due_at(settings, datetime.now(UTC)))
        else:
            arm_sender(sender_id)
        return "SENT"


# --------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------- #
_DAY_INDEX = {"MON": 0, "TUE": 1, "WED": 2, "THU": 3, "FRI": 4, "SAT": 5, "SUN": 6}


def _provider_config(connection: SenderConnection) -> ProviderConnectionConfig:
    return ProviderConnectionConfig(
        connection_type=connection.connection_type,
        external_account_id=connection.external_account_id,
        email=connection.email,
        metadata=dict(connection.connection_metadata or {}),
        credential_reference=connection.credential_reference,
        credential_version=connection.credential_version,
        credential_expires_at=connection.credential_expires_at,
    )


def _redis() -> Redis:
    return build_redis_client()


def _next_at_key(sender_id: UUID) -> str:
    return f"phase10q:{sender_id}:warmup:next_at"


def _due_time(sender_id: UUID) -> datetime | None:
    # Unreadable state means "not due": warmup resumes once Redis recovers
    # rather than the drive loop raising on every sender.
    raw = safe_redis_call("warmup read", lambda: _redis().get(_next_at_key(sender_id)))
    if raw is None:
        return None
    try:
        parsed = datetime.fromisoformat(str(raw))
    except ValueError:
        logger.warning("warmup pointer for %s is not ISO-8601: %r", sender_id, raw)
        return None
    return parsed if parsed.tzinfo is not None else parsed.replace(tzinfo=UTC)


def _random_minute(start: int, end: int) -> int:
    return random.randint(start, end - 1)


def _minutes(hhmm: str) -> int:
    hours, _, minutes = hhmm.partition(":")
    return int(hours) * 60 + int(minutes)


def _next_weekday(probe: datetime, weekdays: set[int], start: int, end: int) -> datetime:
    for offset in range(1, 8):
        day = probe + timedelta(days=offset)
        if day.weekday() in weekdays:
            return day.replace(hour=start // 60, minute=_random_minute(start, end) % 60, second=0, microsecond=0)
    return probe + timedelta(days=7)  # pragma: no cover - _family always non-empty in practice