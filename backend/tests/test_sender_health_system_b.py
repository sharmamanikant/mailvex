"""Phase 10Q System B sender-account health + telemetry writers.

Tests the pure health evaluator (state machine over connection status +
send telemetry) and the ``SenderHealthService`` writers that keep
``SenderAccount.health_status`` / ``last_success_at`` / ``last_failure_at`` /
``last_error`` current.
"""

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
)
from app.services.sender_health import SenderHealthService

CONNECTED = "CONNECTED"


def _account(
    *,
    last_success_at: datetime | None = None,
    last_failure_at: datetime | None = None,
    last_error: str | None = None,
    status: str = "ACTIVE",
    health_status: str = "UNKNOWN",
) -> SenderAccount:
    return SenderAccount(
        id=uuid4(),
        tenant_id=uuid4(),
        connection_id=uuid4(),
        email="sender@example.com",
        status=status,
        health_status=health_status,
        last_success_at=last_success_at,
        last_failure_at=last_failure_at,
        last_error=last_error,
    )


def _connection(status: str = CONNECTED) -> SenderConnection:
    return SenderConnection(
        id=uuid4(),
        tenant_id=uuid4(),
        provider="GOOGLE",
        connection_type="OAUTH",
        status=status,
    )


def _hours_ago(hours: float) -> datetime:
    return datetime.now(UTC) - timedelta(hours=hours)


def _days_ago(days: int) -> datetime:
    return datetime.now(UTC) - timedelta(days=days)


def test_reauth_required_connection_wins() -> None:
    account = _account(last_success_at=_hours_ago(1))
    assert SenderHealthService.evaluate(account, _connection("REAUTH_REQUIRED")) == "REAUTH_REQUIRED"


def test_disconnected_or_failed_connection_is_failed() -> None:
    for status in ("DISCONNECTED", "FAILED", "DISABLED"):
        assert SenderHealthService.evaluate(_account(), _connection(status)) == "FAILED"


def test_non_active_sender_is_failed() -> None:
    account = _account(status="DISABLED")
    assert SenderHealthService.evaluate(account, _connection()) == "FAILED"


def test_no_telemetry_is_assessing() -> None:
    assert SenderHealthService.evaluate(_account(), _connection()) == "ASSESSING"


def test_recent_failure_is_failed() -> None:
    account = _account(last_success_at=_days_ago(10), last_failure_at=_hours_ago(2), last_error="RATE_LIMITED")
    assert SenderHealthService.evaluate(account, _connection()) == "FAILED"


def test_failure_after_success_is_failed() -> None:
    account = _account(last_success_at=_hours_ago(5), last_failure_at=_hours_ago(1))
    assert SenderHealthService.evaluate(account, _connection()) == "FAILED"


def test_old_failure_without_success_is_degraded() -> None:
    account = _account(last_failure_at=_days_ago(8))
    assert SenderHealthService.evaluate(account, _connection()) == "DEGRADED"


def test_fresh_success_is_healthy() -> None:
    account = _account(last_success_at=_hours_ago(3))
    assert SenderHealthService.evaluate(account, _connection()) == "HEALTHY"


def test_stale_success_is_degraded() -> None:
    account = _account(last_success_at=_days_ago(30))
    assert SenderHealthService.evaluate(account, _connection()) == "DEGRADED"


# --------------------------------------------------------------------- #
# Telemetry writers (DB-backed)
# --------------------------------------------------------------------- #
@pytest.fixture()
def health_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'sender_health.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    tenant = Tenant(name="Health", slug=f"h-{uuid4().hex[:8]}")
    session.add(tenant)
    session.commit()

    connection = SenderConnection(
        tenant_id=tenant.id,
        provider="GOOGLE",
        connection_type="OAUTH",
        status="CONNECTED",
    )
    session.add(connection)
    session.commit()
    account = SenderAccount(
        tenant_id=tenant.id,
        connection_id=connection.id,
        provider="GOOGLE",
        email="sender@example.com",
    )
    session.add(account)
    session.commit()
    session.close()

    yield session_factory, tenant.id, connection.id
    engine.dispose()


def test_record_success_marks_healthy(health_session) -> None:
    session_factory, tenant_id, _ = health_session
    session = session_factory()
    account = session.query(SenderAccount).one()
    SenderHealthService(session, tenant_id).record_send_outcome(account.id, success=True, message_id="accepted:abc")
    session.refresh(account)
    assert account.last_success_at is not None
    assert account.last_error is None
    assert account.health_status == "HEALTHY"
    session.close()


def test_record_failure_marks_failed_with_error(health_session) -> None:
    session_factory, tenant_id, _ = health_session
    session = session_factory()
    account = session.query(SenderAccount).one()
    SenderHealthService(session, tenant_id).record_send_outcome(
        account.id,
        success=False,
        error_code="AUTH_FAILED",
    )
    session.refresh(account)
    assert account.last_failure_at is not None
    assert account.last_error == "AUTH_FAILED"
    assert account.health_status == "FAILED"
    session.close()


def test_failure_after_success_still_failed_then_recovers(health_session) -> None:
    session_factory, tenant_id, _ = health_session
    session = session_factory()
    account = session.query(SenderAccount).one()
    service = SenderHealthService(session, tenant_id)
    service.record_send_outcome(account.id, success=True)
    service.record_send_outcome(account.id, success=False, error_code="RATE_LIMITED")
    session.refresh(account)
    assert account.health_status == "FAILED"
    assert account.last_error == "RATE_LIMITED"
    # A later success flips health back to HEALTHY and clears the error.
    service.record_send_outcome(account.id, success=True)
    session.refresh(account)
    assert account.health_status == "HEALTHY"
    assert account.last_error is None
    session.close()


def test_refresh_recomputes_from_connection(health_session) -> None:
    session_factory, tenant_id, connection_id = health_session
    session = session_factory()
    session.query(SenderConnection).filter_by(id=connection_id).update({"status": "REAUTH_REQUIRED"})
    session.commit()
    account = session.query(SenderAccount).one()
    SenderHealthService(session, tenant_id).refresh(account.id)
    session.refresh(account)
    assert account.health_status == "REAUTH_REQUIRED"
    session.close()