"""Phase 10Q sender quotas (Redis-required) and the warmup queue service."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (
    Base,
    SenderAccount,
    SenderConnection,
    Tenant,
    WarmupSettings,
)
from app.security.rate_limit import RateLimitResult
from app.services import warmup as warmup_module
from app.services.sender_health import SenderHealthService
from app.services.sender_quotas import SenderQuotaExceeded, SenderQuotaService
from app.services.warmup import WarmupSendBlocked, WarmupService
from tests.test_sender_health_system_b import _account as _health_account


# --------------------------------------------------------------------- #
# Quota service (unit)
# --------------------------------------------------------------------- #
class _Limiter:
    def __init__(self, allowed: bool = True) -> None:
        self.allowed = allowed
        self.keys: list[str] = []

    def check_limit(self, key: str, limit: int, window: int) -> RateLimitResult:
        self.keys.append(key)
        if not self.allowed:
            return RateLimitResult(False, window)
        return RateLimitResult(True, 0)


def _quota(limiter: _Limiter) -> SenderQuotaService:
    service = SenderQuotaService.__new__(SenderQuotaService)
    service.rate_limits = limiter  # type: ignore[assignment]
    return service


def _unlimited_account() -> SenderAccount:
    account = _health_account()
    account.daily_campaign_limit = None
    account.daily_warmup_limit = None
    return account


def _capped_account(limit: int) -> SenderAccount:
    account = _health_account()
    account.daily_campaign_limit = limit
    account.daily_warmup_limit = limit
    return account


def test_uncapped_sender_never_touches_redis() -> None:
    limiter = _Limiter()
    service = _quota(limiter)
    service.reserve_campaign(_unlimited_account())
    service.reserve_warmup(_unlimited_account())
    assert limiter.keys == []


def test_capped_sender_reserves_both_kinds_up_to_limit() -> None:
    limiter = _Limiter()
    service = _quota(limiter)
    account = _capped_account(2)
    service.reserve_campaign(account)
    service.reserve_warmup(account)
    assert len(limiter.keys) == 2
    assert "phase10q:" in limiter.keys[0]


def test_exhausted_daily_limit_raises_quota_error() -> None:
    service = _quota(_Limiter(allowed=False))
    with pytest.raises(SenderQuotaExceeded) as excinfo:
        service.reserve_warmup(_capped_account(1))
    assert excinfo.value.kind == "warmup"
    assert excinfo.value.limit == 1


def test_denied_redis_result_fails_closed_into_quota_error() -> None:
    service = _quota(_Limiter(allowed=False))
    with pytest.raises(SenderQuotaExceeded):
        service.reserve_campaign(_capped_account(5))


def test_redis_outage_propagates_as_hard_failure() -> None:
    class Outage:
        def check_limit(self, *_args: object, **_kwargs: object) -> RateLimitResult:
            return RateLimitResult(False, 86_400)

    with pytest.raises(SenderQuotaExceeded):
        _quota(Outage()).reserve_warmup(_capped_account(5))  # type: ignore[arg-type]


# --------------------------------------------------------------------- #
# Warmup scheduling (pure)
# --------------------------------------------------------------------- #
def _settings(account: SenderAccount) -> WarmupSettings:
    return WarmupSettings(
        tenant_id=account.tenant_id,
        sender_id=account.id,
        start_time="09:00",
        end_time="17:00",
        weekdays=["MON", "TUE", "WED", "THU", "FRI"],
        minimum_delay=10,
        maximum_delay=10,
        enabled=True,
    )


def test_next_due_crossing_end_of_window_rolls_to_monday() -> None:
    service = object.__new__(WarmupService)
    account = _health_account()
    settings = _settings(account)
    friday_late = datetime(2026, 9, 4, 16, 55, tzinfo=UTC)  # Friday 16:55 + 10min -> 17:05 (past 17:00)
    nxt = service.next_due_at(settings, friday_late)
    assert nxt.weekday() == 0  # rolls to Monday
    assert 9 <= nxt.hour < 17


def test_next_due_after_window_rolls_to_next_weekday() -> None:
    service = object.__new__(WarmupService)
    settings = _settings(_health_account())
    friday_late = datetime(2026, 9, 4, 18, 10, tzinfo=UTC)
    nxt = service.next_due_at(settings, friday_late)
    assert nxt.weekday() == 0  # Monday
    assert 9 <= nxt.hour < 17


def test_next_due_before_window_clamps_to_start() -> None:
    service = object.__new__(WarmupService)
    settings = _settings(_health_account())
    early = datetime(2026, 9, 7, 8, 0, tzinfo=UTC)  # Monday 08:00
    nxt = service.next_due_at(settings, early)
    assert nxt.hour == 9 and nxt.minute == 0
    assert nxt.weekday() == 0


def test_next_due_respects_weekday_allowlist() -> None:
    service = object.__new__(WarmupService)
    account = _health_account()
    settings = _settings(account)
    settings.weekdays = ["SAT"]
    saturday = datetime(2026, 9, 5, 12, 0, tzinfo=UTC)
    assert service.next_due_at(settings, saturday) <= saturday + timedelta(hours=1)


# --------------------------------------------------------------------- #
# Warmup send (DB-backed with fake provider + fake redis)
# --------------------------------------------------------------------- #
class _FakeProvider:
    def __init__(self) -> None:
        self.sent: list[str] = []

    def send_message(self, _config, message) -> str:
        self.sent.append(message.to[0])
        return f"warmup:{len(self.sent)}"


class _FakeRedis:
    def __init__(self) -> None:
        self.store: dict[str, str] = {}

    def get(self, key: str) -> str | None:
        return self.store.get(key)

    def set(self, key: str, value: str, ex: int | None = None) -> None:
        self.store[key] = value

    def delete(self, *keys: str) -> None:
        for key in keys:
            self.store.pop(key, None)


@pytest.fixture()
def warmup_app(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'warmup.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    tenant = Tenant(name="Warmup", slug=f"w-{uuid4().hex[:8]}")
    session.add(tenant)
    session.commit()

    connection = SenderConnection(
        tenant_id=tenant.id,
        provider="SENDGRID",
        connection_type="CREDENTIAL",
        status="CONNECTED",
    )
    session.add(connection)
    session.flush()
    account = SenderAccount(
        tenant_id=tenant.id,
        connection_id=connection.id,
        provider="SENDGRID",
        email="warmup@example.com",
        status="ACTIVE",
        health_status="UNKNOWN",
        warmup_enabled=True,
        daily_warmup_limit=10,
    )
    session.add(account)
    session.commit()
    session.close()

    fake_redis = _FakeRedis()
    fake_redis.store[f"phase10q:{account.id}:warmup:next_at"] = datetime.now(UTC).isoformat()
    monkeypatch.setattr(warmup_module, "_redis", lambda: fake_redis)
    fake_provider = _FakeProvider()
    monkeypatch.setattr("app.email_providers.get_provider", lambda _name: fake_provider)

    def make_service(allow: bool):
        limiter = _Limiter(allowed=allow)
        return WarmupService(
            session_factory(),
            tenant.id,
            _quota(limiter),
            SenderHealthService(session_factory(), tenant.id),
        )

    yield session_factory, account.id, fake_provider, fake_redis, make_service
    engine.dispose()


def test_warmup_send_succeeds_and_rearms(warmup_app) -> None:
    session_factory, account_id, provider, redis, make_service = warmup_app
    status = make_service(True).send_message(account_id)
    assert status == "SENT"
    assert provider.sent == ["warmup@example.com"]
    assert redis.store.get(f"phase10q:{account_id}:warmup:next_at") is not None
    session = session_factory()
    account = session.get(SenderAccount, account_id)
    assert account.last_success_at is not None
    assert account.health_status == "HEALTHY"
    session.close()


def test_warmup_send_blocked_when_disabled(warmup_app) -> None:
    session_factory, account_id, _provider, redis, make_service = warmup_app
    session = session_factory()
    account = session.get(SenderAccount, account_id)
    account.warmup_enabled = False
    session.commit()
    session.close()
    with pytest.raises(WarmupSendBlocked):
        make_service(True).send_message(account_id)
    assert f"phase10q:{account_id}:warmup:next_at" not in redis.store


def test_warmup_send_blocked_when_reauth_required(warmup_app) -> None:
    session_factory, account_id, _provider, redis, make_service = warmup_app
    session = session_factory()
    connection_id = session.get(SenderAccount, account_id).connection_id
    session.query(SenderConnection).filter_by(id=connection_id).update({"status": "REAUTH_REQUIRED"})
    session.commit()
    session.close()
    with pytest.raises(WarmupSendBlocked):
        make_service(True).send_message(account_id)
    assert f"phase10q:{account_id}:warmup:next_at" not in redis.store


def test_warmup_send_blocked_on_quota_exhaustion(warmup_app) -> None:
    _session_factory, account_id, _provider, _redis, make_service = warmup_app
    with pytest.raises(WarmupSendBlocked):
        make_service(False).send_message(account_id)


def test_warmup_send_records_failure_telemetry(warmup_app, monkeypatch) -> None:
    session_factory, account_id, _provider, _redis, make_service = warmup_app

    class _Failing:
        def send_message(self, _config, _message) -> str:
            raise RuntimeError("boom")

    monkeypatch.setattr("app.email_providers.get_provider", lambda _name: _Failing())
    with pytest.raises(WarmupSendBlocked):
        make_service(True).send_message(account_id)
    session = session_factory()
    account = session.get(SenderAccount, account_id)
    assert account.last_failure_at is not None
    assert account.last_error == "PROVIDER_UNAVAILABLE"
    session.close()


def test_drive_enqueues_only_due_senders(warmup_app) -> None:
    _session_factory, account_id, _provider, redis, make_service = warmup_app
    service = make_service(True)
    enqueued: list[str] = []
    due = service.drive(lambda t, s: enqueued.append(s))
    assert due == [account_id]
    assert enqueued == [str(account_id)]

    redis.store[f"phase10q:{account_id}:warmup:next_at"] = (
        datetime.now(UTC) + timedelta(hours=2)
    ).isoformat()
    assert service.drive(lambda t, s: enqueued.append(s)) == []
    assert len(enqueued) == 1


def test_drive_enqueues_stale_past_next_at(warmup_app) -> None:
    _session_factory, account_id, _provider, redis, make_service = warmup_app
    redis.store[f"phase10q:{account_id}:warmup:next_at"] = (
        datetime.now(UTC) - timedelta(days=2)
    ).isoformat()
    service = make_service(True)
    enqueued: list[str] = []
    due = service.drive(lambda t, s: enqueued.append(s))
    assert due == [account_id]
    assert enqueued == [str(account_id)]


def test_drive_disarms_future_orphan_next_at(warmup_app) -> None:
    _session_factory, account_id, _provider, redis, make_service = warmup_app
    redis.store[f"phase10q:{account_id}:warmup:next_at"] = (
        datetime.now(UTC) + timedelta(hours=36)
    ).isoformat()
    service = make_service(True)
    service.drive(lambda t, s: None)
    assert f"phase10q:{account_id}:warmup:next_at" not in redis.store