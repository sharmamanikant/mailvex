"""Unit + integration tests for the Phase 5 Sender health engine.

Covers domain extraction, DNS normalization, every provider-independent check,
the deterministic scoring rules (weights, unknown redistribution, thresholds,
overrides), and the ``SenderHealthService`` orchestrator against an isolated
SQLite database with a static DNS resolver (no live DNS, no Redis).
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select, update
from sqlalchemy.orm import sessionmaker

from app.models import (
    Base,
    Mailbox,
    ProviderConnection,
    Sender,
    SenderHealthCheck,
    SenderHealthCheckResult,
    Tenant,
)
from app.security.rate_limit import RateLimitResult
from app.services.sender_health_engine.checks import (
    dkim_check,
    dmarc_check,
    dns_mx_check,
    domain_check,
    mailbox_status_check,
    provider_connection_check,
    sending_configuration_check,
    spf_check,
)
from app.services.sender_health_engine.dns import StaticDnsResolver
from app.services.sender_health_engine.domain import extract_domain
from app.services.sender_health_engine.orchestrator import SenderHealthService
from app.services.sender_health_engine.providers import build_provider_adapter
from app.services.sender_health_engine.scoring import (
    THRESHOLD_HEALTHY,
    THRESHOLD_WARNING,
    WEIGHTS,
    aggregate_score,
    overall_status,
    status_from_score,
)
from app.services.sender_health_engine.types import (
    CheckContext,
    CheckResult,
    SenderHealthError,
)
from app.services.workspace_senders import SenderNotFoundError


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #
class FakeSender:
    def __init__(self, email: str = "sender@example.com", provider: str = "GOOGLE") -> None:
        self.tenant_id = uuid4()
        self.id = uuid4()
        self.provider = provider
        self.status = "ACTIVE"
        self.sending_enabled = True
        self.email = email


class FakeMailbox:
    provider_status: str = "ACTIVE"
    is_suspended: bool = False
    is_deleted: bool = False


class FakeProvider:
    status: str = "CONNECTED"
    credential_reference: str = "encrypted:test"
    workspace_domain: str = "example.com"


_MISSING = object()


def _ctx(
    *,
    sender: FakeSender | None = None,
    mailbox: FakeMailbox | None = _MISSING,
    connection: FakeProvider | None = _MISSING,
    resolver: StaticDnsResolver | None = None,
) -> CheckContext:
    sender = sender or FakeSender()
    return CheckContext(
        sender=sender,  # type: ignore[arg-type]
        mailbox=mailbox if mailbox is not _MISSING else FakeMailbox(),  # type: ignore[arg-type]
        connection=connection if connection is not _MISSING else FakeProvider(),  # type: ignore[arg-type]
        domain=extract_domain(sender.email),
        resolver=resolver or StaticDnsResolver(),
        provider_adapter=build_provider_adapter(sender.provider),
        now=datetime.now(UTC),
    )


def _result(check_type: str, status: str, score: int | None, severity: str = "INFO") -> CheckResult:
    return CheckResult(
        check_type,  # type: ignore[arg-type]
        status,  # type: ignore[arg-type]
        title=f"{check_type}: {status}",
        severity=severity,  # type: ignore[arg-type]
        score=score,
    )


HEALTHY_DNS = StaticDnsResolver(
    txt={
        "example.com": ["v=spf1 include:_spf.google.com ~all"],
        "google._domainkey.example.com": ["v=DKIM1; k=rsa; p=abc"],
        "_dmarc.example.com": ["v=DMARC1; p=reject; rua=mailto:dmarc@example.com"],
    },
    mx={"example.com": ["mail.example.com"]},
)


# ===================================================================== #
# Domain extraction
# ===================================================================== #
def test_extract_domain_normalizes_email() -> None:
    assert extract_domain("Sender@Example.COM ") == "example.com"
    assert extract_domain("sender@example.com.") == "example.com"
    assert extract_domain("Sender.Name+tag@example.com") == "example.com"
    assert extract_domain("sender@例子.公司") == "xn--fsqu00a.xn--55qx5d"


def test_extract_domain_rejects_malformed() -> None:
    for bad in ("", "   ", "no-at-sign", "user@", "@example.com", "user@-bad.com", "user@example..com"):
        assert extract_domain(bad) is None


# ===================================================================== #
# SPF
# ===================================================================== #
def test_spf_pass_single_record() -> None:
    result = spf_check(_ctx(resolver=HEALTHY_DNS))
    assert result.status == "PASS"
    assert result.score == 100
    assert result.metadata["record_count"] == 1


def test_spf_multiple_records_warns() -> None:
    resolver = StaticDnsResolver(txt={"example.com": ["v=spf1 a", "v=spf1 include:_spf.google.com ~all"]})
    result = spf_check(_ctx(resolver=resolver))
    assert result.status == "WARNING"
    assert result.score == 60


def test_spf_missing_fails_high() -> None:
    result = spf_check(_ctx(resolver=StaticDnsResolver(txt={"example.com": ["v=DKIM1; p=abc"]})))
    assert result.status == "FAIL"
    assert result.severity == "HIGH"
    assert result.score == 0


def test_spf_dns_error_is_unknown_not_fail() -> None:
    resolver = StaticDnsResolver(errors={"example.com": "TIMEOUT"})
    result = spf_check(_ctx(resolver=resolver))
    assert result.status == "UNKNOWN"
    assert result.metadata["error"] == "TIMEOUT"


# ===================================================================== #
# DKIM
# ===================================================================== #
def test_dkim_pass_google_selector() -> None:
    result = dkim_check(_ctx(resolver=HEALTHY_DNS))
    assert result.status == "PASS"
    assert result.metadata["selector"] == "google"


def test_dkim_key_missing_fails_high() -> None:
    resolver = StaticDnsResolver(txt={"example.com": ["v=spf1 ..."]})
    result = dkim_check(_ctx(resolver=resolver))
    assert result.status == "FAIL"
    assert result.severity == "HIGH"
    assert result.score == 0


def test_dkim_unknown_without_selector() -> None:
    ctx = _ctx(sender=FakeSender(provider="MICROSOFT"))
    result = dkim_check(ctx)
    assert result.status == "UNKNOWN"
    assert result.metadata["selector_discovered"] is False


def test_dkim_dns_error_unknown() -> None:
    resolver = StaticDnsResolver(errors={"google._domainkey.example.com": "SERVFAIL"})
    result = dkim_check(_ctx(resolver=resolver))
    assert result.status == "UNKNOWN"
    assert result.metadata["error"] == "SERVFAIL"


# ===================================================================== #
# DMARC
# ===================================================================== #
def test_dmarc_pass_enforced_policy() -> None:
    result = dmarc_check(_ctx(resolver=HEALTHY_DNS))
    assert result.status == "PASS"
    assert result.score == 100
    assert result.metadata["p"] == "reject"


def test_dmarc_none_policy_warns() -> None:
    resolver = StaticDnsResolver(txt={"_dmarc.example.com": ["v=DMARC1; p=none"]})
    result = dmarc_check(_ctx(resolver=resolver))
    assert result.status == "WARNING"
    assert result.score == 60


def test_dmarc_missing_policy_tag_warns() -> None:
    resolver = StaticDnsResolver(txt={"_dmarc.example.com": ["v=DMARC1; pct=100"]})
    result = dmarc_check(_ctx(resolver=resolver))
    assert result.status == "WARNING"
    assert result.title == "DMARC policy tag is missing or malformed"


def test_dmarc_missing_record_fails_high() -> None:
    result = dmarc_check(_ctx(resolver=StaticDnsResolver()))
    assert result.status == "FAIL"
    assert result.severity == "HIGH"
    assert result.score == 0


def test_dmarc_dns_error_unknown() -> None:
    resolver = StaticDnsResolver(errors={"_dmarc.example.com": "NXDOMAIN"})
    result = dmarc_check(_ctx(resolver=resolver))
    assert result.status == "UNKNOWN"


# ===================================================================== #
# MX (DNS)
# ===================================================================== #
def test_mx_present_passes() -> None:
    result = dns_mx_check(_ctx(resolver=HEALTHY_DNS))
    assert result.status == "PASS"


def test_mx_missing_warns_low_stakes() -> None:
    result = dns_mx_check(_ctx(resolver=StaticDnsResolver()))
    assert result.status == "WARNING"
    assert result.score == 50


def test_mx_error_unknown() -> None:
    resolver = StaticDnsResolver(errors={"example.com": "TIMEOUT"})
    result = dns_mx_check(_ctx(resolver=resolver))
    assert result.status == "UNKNOWN"


# ===================================================================== #
# DOMAIN
# ===================================================================== #
def test_domain_pass() -> None:
    assert domain_check(_ctx()).status == "PASS"


def test_domain_invalid_fails() -> None:
    ctx = _ctx(sender=FakeSender(email="not-an-email"))
    result = domain_check(ctx)
    assert result.status == "FAIL"
    assert result.severity == "HIGH"
    assert result.score == 0


# ===================================================================== #
# PROVIDER_CONNECTION
# ===================================================================== #
def test_provider_connected_pass() -> None:
    result = provider_connection_check(_ctx())
    assert result.status == "PASS"
    assert result.metadata["credential_configured"] is True


def test_provider_revoked_fails_critical() -> None:
    conn = FakeProvider()
    conn.status = "REVOKED"
    result = provider_connection_check(_ctx(connection=conn))
    assert result.status == "FAIL"
    assert result.severity == "CRITICAL"
    assert result.score == 0


def test_provider_disconnected_fails_critical() -> None:
    conn = FakeProvider()
    conn.status = "DISCONNECTED"
    result = provider_connection_check(_ctx(connection=conn))
    assert result.status == "FAIL"
    assert result.severity == "CRITICAL"


def test_provider_error_fails_high() -> None:
    conn = FakeProvider()
    conn.status = "ERROR"
    result = provider_connection_check(_ctx(connection=conn))
    assert result.status == "FAIL"
    assert result.severity == "HIGH"


def test_provider_connecting_or_no_credential_warns() -> None:
    conn = FakeProvider()
    conn.status = "CONNECTING"
    assert provider_connection_check(_ctx(connection=conn)).status == "WARNING"
    conn.status = "CONNECTED"
    conn.credential_reference = ""
    assert provider_connection_check(_ctx(connection=conn)).status == "WARNING"


def test_provider_missing_fails_critical() -> None:
    result = provider_connection_check(_ctx(connection=None))
    assert result.status == "FAIL"
    assert result.severity == "CRITICAL"


# ===================================================================== #
# MAILBOX_STATUS
# ===================================================================== #
def test_mailbox_active_passes() -> None:
    assert mailbox_status_check(_ctx()).status == "PASS"


def test_mailbox_suspended_fails() -> None:
    mailbox = FakeMailbox()
    mailbox.is_suspended = True
    result = mailbox_status_check(_ctx(mailbox=mailbox))
    assert result.status == "FAIL"
    assert result.severity == "CRITICAL"


def test_mailbox_deleted_fails() -> None:
    mailbox = FakeMailbox()
    mailbox.is_deleted = True
    result = mailbox_status_check(_ctx(mailbox=mailbox))
    assert result.status == "FAIL"
    assert result.severity == "CRITICAL"


def test_mailbox_non_active_status_warns() -> None:
    mailbox = FakeMailbox()
    mailbox.provider_status = "PROVISIONING"
    result = mailbox_status_check(_ctx(mailbox=mailbox))
    assert result.status == "WARNING"
    assert result.score == 50


def test_mailbox_missing_fails() -> None:
    result = mailbox_status_check(_ctx(mailbox=None))
    assert result.status == "FAIL"
    assert result.severity == "CRITICAL"


# ===================================================================== #
# SENDING_CONFIGURATION
# ===================================================================== #
def test_sending_configuration_available() -> None:
    result = sending_configuration_check(_ctx())
    assert result.status == "PASS"
    assert result.score == 100


def test_sending_disabled_warns() -> None:
    sender = FakeSender()
    sender.sending_enabled = False
    result = sending_configuration_check(_ctx(sender=sender))
    assert result.status == "WARNING"
    assert result.metadata["reason"] == "SENDER_DISABLED_SENDING"


def test_sending_sender_removed_warns() -> None:
    sender = FakeSender()
    sender.status = "REMOVED"
    result = sending_configuration_check(_ctx(sender=sender))
    assert result.status == "WARNING"
    assert result.metadata["reason"] == "SENDER_REMOVED"


def test_sending_mailbox_unavailable_warns() -> None:
    mailbox = FakeMailbox()
    mailbox.provider_status = "SUSPENDED"
    result = sending_configuration_check(_ctx(mailbox=mailbox))
    assert result.status == "WARNING"
    assert result.metadata["reason"] == "MAILBOX_UNAVAILABLE"


def test_sending_provider_disconnected_warns() -> None:
    conn = FakeProvider()
    conn.status = "DISCONNECTED"
    result = sending_configuration_check(_ctx(connection=conn))
    assert result.status == "WARNING"
    assert result.metadata["reason"] == "PROVIDER_DISCONNECTED"


# ===================================================================== #
# Scoring
# ===================================================================== #
def test_weights_sum_to_100() -> None:
    assert sum(WEIGHTS.values()) == pytest.approx(100.0)


def test_all_pass_scores_100() -> None:
    passes = [_result(t, "PASS", 100) for t in WEIGHTS]
    score, contributions = aggregate_score(passes)
    assert score == pytest.approx(100.0)
    assert sum(contributions.values()) == pytest.approx(100.0)


def test_unknown_check_redistributes_weight() -> None:
    results = [_result(t, "PASS", 100) for t in WEIGHTS]
    results.append(_result("SENDING_SIGNALS", "UNKNOWN", None))
    score, contributions = aggregate_score(results)
    assert score == pytest.approx(100.0)
    assert sum(contributions.values()) == pytest.approx(100.0)


def test_unknown_never_punished_as_zero() -> None:
    results = [
        _result("PROVIDER_CONNECTION", "PASS", 100),
        _result("SENDING_SIGNALS", "UNKNOWN", None),
    ]
    score, contributions = aggregate_score(results)
    assert score == pytest.approx(100.0)
    assert contributions == {"PROVIDER_CONNECTION": 100.0}


def test_all_unknown_is_none() -> None:
    results = [_result(t, "UNKNOWN", None) for t in WEIGHTS]
    score, contributions = aggregate_score(results)
    assert score is None
    assert contributions == {}


def test_threshold_boundaries() -> None:
    assert status_from_score(THRESHOLD_HEALTHY) == "HEALTHY"
    assert status_from_score(THRESHOLD_WARNING) == "WARNING"
    assert status_from_score(79.99) == "WARNING"
    assert status_from_score(59.99) == "CRITICAL"


def test_critical_failure_forces_critical() -> None:
    results = [
        _result("PROVIDER_CONNECTION", "FAIL", 0, severity="CRITICAL"),
        _result("MAILBOX_STATUS", "PASS", 100),
        _result("SPF", "PASS", 100),
        _result("DKIM", "PASS", 100),
        _result("DMARC", "PASS", 100),
        _result("DNS", "PASS", 100),
        _result("SENDING_CONFIGURATION", "PASS", 100),
        _result("SENDING_SIGNALS", "PASS", 100),
    ]
    score, _ = aggregate_score(results)
    assert score == pytest.approx(80.0)  # weighted average still valid
    assert overall_status(results, score) == "CRITICAL"


def test_any_fail_forces_at_most_warning() -> None:
    results = [
        _result("PROVIDER_CONNECTION", "PASS", 100),
        _result("MAILBOX_STATUS", "PASS", 100),
        _result("SPF", "FAIL", 0, severity="HIGH"),
        _result("DKIM", "PASS", 100),
        _result("DMARC", "PASS", 100),
        _result("DNS", "PASS", 100),
        _result("SENDING_CONFIGURATION", "PASS", 100),
        _result("SENDING_SIGNALS", "PASS", 100),
    ]
    score, _ = aggregate_score(results)
    assert score == pytest.approx(85.0)
    assert overall_status(results, score) == "WARNING"


def test_low_score_without_fail_is_warning() -> None:
    results = [
        _result("PROVIDER_CONNECTION", "PASS", 100),
        _result("MAILBOX_STATUS", "WARNING", 50),
        _result("SPF", "WARNING", 60),
        _result("DKIM", "PASS", 100),
        _result("DMARC", "WARNING", 60),
        _result("DNS", "PASS", 100),
        _result("SENDING_CONFIGURATION", "PASS", 100),
        _result("SENDING_SIGNALS", "PASS", 100),
    ]
    score, _ = aggregate_score(results)
    assert THRESHOLD_WARNING <= score < THRESHOLD_HEALTHY
    assert overall_status(results, score) == "WARNING"


def test_no_known_checks_is_unknown_overall() -> None:
    results = [_result(t, "UNKNOWN", None) for t in WEIGHTS]
    assert overall_status(results, None) == "UNKNOWN"


# ===================================================================== #
# Orchestrator integration (SQLite + static DNS, no Redis)
# ===================================================================== #
@pytest.fixture()
def engine_session(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'health.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()
    tenant = Tenant(name="Alpha", slug=f"alpha-{uuid4().hex[:8]}")
    session.add(tenant)
    session.flush()
    yield session, tenant.id
    session.close()


def _seed_sender(session, tenant_id, *, connection_status="CONNECTED", sending_enabled=True):
    connection = ProviderConnection(
        tenant_id=tenant_id,
        provider="GOOGLE",
        connection_type="OAUTH",
        status=connection_status,
        provider_account_id="acct-1",
        workspace_domain="example.com",
        credential_reference="encrypted:never-export",
    )
    session.add(connection)
    session.flush()
    mailbox = Mailbox(
        tenant_id=tenant_id,
        provider_connection_id=connection.id,
        provider_mailbox_id="m1",
        email="sender@example.com",
        provider_status="ACTIVE",
    )
    session.add(mailbox)
    session.flush()
    sender = Sender(
        tenant_id=tenant_id,
        mailbox_id=mailbox.id,
        provider_connection_id=connection.id,
        email="sender@example.com",
        display_name="Sender",
        provider="GOOGLE",
        status="ACTIVE",
        sending_enabled=sending_enabled,
        health_status="UNKNOWN",
    )
    session.add(sender)
    session.commit()
    return sender


class _DenyLimiter:
    def check_limit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        return RateLimitResult(False, 60)


def test_run_healthy_scores_and_persists(engine_session) -> None:
    session, tenant_id = engine_session
    sender = _seed_sender(session, tenant_id)
    payload = SenderHealthService(session, tenant_id, resolver=HEALTHY_DNS).run_health_check(
        sender.id, triggered_by="MANUAL"
    )
    assert payload["overall_status"] == "HEALTHY"
    assert payload["overall_score"] == 100
    assert payload["score_version"] == "v1"
    assert payload["triggered_by"] == "MANUAL"
    assert payload["duration_ms"] >= 0
    assert len(payload["results"]) == 9
    statuses = {r["check_type"]: r["status"] for r in payload["results"]}
    assert statuses["SENDING_SIGNALS"] == "UNKNOWN"
    assert payload["score_explanation"]["weights"]["PROVIDER_CONNECTION"] == 20.0
    assert payload["score_explanation"]["thresholds"] == {"healthy": 80.0, "warning": 60.0}

    session.refresh(sender)
    assert sender.health_status == "HEALTHY"
    assert float(sender.health_score) == 100.0
    assert sender.last_health_check_at is not None

    check_count = session.scalar(select(func.count(SenderHealthCheck.id)).where(SenderHealthCheck.tenant_id == tenant_id))
    result_count = session.scalar(
        select(func.count(SenderHealthCheckResult.id)).where(SenderHealthCheckResult.tenant_id == tenant_id)
    )
    assert check_count == 1
    assert result_count == 9


def test_run_does_not_leak_secrets(engine_session) -> None:
    session, tenant_id = engine_session
    sender = _seed_sender(session, tenant_id)
    payload = SenderHealthService(session, tenant_id, resolver=HEALTHY_DNS).run_health_check(
        sender.id, triggered_by="MANUAL"
    )
    serialized = str(payload)
    for forbidden in ("credential_reference", "encrypted:", "access_token", "refresh_token", "client_secret"):
        assert forbidden not in serialized


def test_run_revoked_connection_is_critical(engine_session) -> None:
    session, tenant_id = engine_session
    sender = _seed_sender(session, tenant_id, connection_status="REVOKED")
    payload = SenderHealthService(session, tenant_id, resolver=HEALTHY_DNS).run_health_check(
        sender.id, triggered_by="MANUAL"
    )
    assert payload["overall_status"] == "CRITICAL"
    provider = next(r for r in payload["results"] if r["check_type"] == "PROVIDER_CONNECTION")
    assert provider["status"] == "FAIL"
    assert provider["severity"] == "CRITICAL"


def test_run_dns_outage_is_unknown_not_crash(engine_session) -> None:
    session, tenant_id = engine_session
    sender = _seed_sender(session, tenant_id)
    resolver = StaticDnsResolver(
        errors={"example.com": "TIMEOUT", "google._domainkey.example.com": "TIMEOUT", "_dmarc.example.com": "TIMEOUT"}
    )
    payload = SenderHealthService(session, tenant_id, resolver=resolver).run_health_check(
        sender.id, triggered_by="SYSTEM"
    )
    statuses = {r["check_type"]: r["status"] for r in payload["results"]}
    for check_type in ("SPF", "DKIM", "DMARC", "DNS"):
        assert statuses[check_type] == "UNKNOWN"
    assert payload["overall_status"] in {"HEALTHY", "WARNING"}


class _AllowLimiter:
    def check_limit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
        return RateLimitResult(True, 0)


def test_rate_limited_manual_check(engine_session) -> None:
    session, tenant_id = engine_session
    sender = _seed_sender(session, tenant_id)
    service = SenderHealthService(session, tenant_id, resolver=HEALTHY_DNS, rate_limiter=_DenyLimiter())
    with pytest.raises(SenderHealthError) as excinfo:
        service.run_health_check(sender.id, triggered_by="MANUAL")
    assert excinfo.value.status_code == 429
    assert excinfo.value.code == "HEALTH_CHECK_RATE_LIMITED"


def test_scheduled_runs_bypasses_manual_rate_limit(engine_session) -> None:
    session, tenant_id = engine_session
    sender = _seed_sender(session, tenant_id)
    service = SenderHealthService(session, tenant_id, resolver=HEALTHY_DNS, rate_limiter=_DenyLimiter())
    payload = service.run_health_check(sender.id, triggered_by="SCHEDULED")
    assert payload["overall_status"] == "HEALTHY"


def test_concurrent_run_conflicts(engine_session) -> None:
    session, tenant_id = engine_session
    sender = _seed_sender(session, tenant_id)
    session.execute(update(Sender).where(Sender.id == sender.id).values(health_status="CHECKING"))
    session.commit()
    service = SenderHealthService(session, tenant_id, resolver=HEALTHY_DNS)
    with pytest.raises(SenderHealthError) as excinfo:
        service.run_health_check(sender.id, triggered_by="MANUAL")
    assert excinfo.value.status_code == 409
    assert excinfo.value.code == "HEALTH_CHECK_IN_PROGRESS"


def test_stale_lock_is_stolen(engine_session) -> None:
    session, tenant_id = engine_session
    sender = _seed_sender(session, tenant_id)
    stale = datetime.now(UTC) - timedelta(days=10)
    session.execute(
        update(Sender).where(Sender.id == sender.id).values(health_status="CHECKING", last_health_check_at=stale)
    )
    session.commit()
    payload = SenderHealthService(session, tenant_id, resolver=HEALTHY_DNS).run_health_check(
        sender.id, triggered_by="MANUAL"
    )
    assert payload["overall_status"] == "HEALTHY"


def test_other_tenant_cannot_see_sender(engine_session) -> None:
    session, tenant_id = engine_session
    sender = _seed_sender(session, tenant_id)
    other_tenant = Tenant(name="Beta", slug=f"beta-{uuid4().hex[:8]}")
    session.add(other_tenant)
    session.commit()
    service = SenderHealthService(session, other_tenant.id, resolver=HEALTHY_DNS)
    with pytest.raises(SenderNotFoundError):
        service.run_health_check(sender.id, triggered_by="MANUAL")


def test_invalid_trigger_rejected(engine_session) -> None:
    session, tenant_id = engine_session
    sender = _seed_sender(session, tenant_id)
    service = SenderHealthService(session, tenant_id, resolver=HEALTHY_DNS)
    with pytest.raises(SenderHealthError) as excinfo:
        service.run_health_check(sender.id, triggered_by="CRON")
    assert excinfo.value.status_code == 400


def test_catastrophic_failure_records_failed_run(engine_session, monkeypatch) -> None:
    session, tenant_id = engine_session
    sender = _seed_sender(session, tenant_id)
    service = SenderHealthService(session, tenant_id, resolver=HEALTHY_DNS)

    def boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(SenderHealthService, "_evaluate", boom)
    with pytest.raises(SenderHealthError) as excinfo:
        service.run_health_check(sender.id, triggered_by="MANUAL")
    assert excinfo.value.status_code == 500
    assert excinfo.value.code == "HEALTH_CHECK_RUN_FAILED"

    failed = session.scalars(
        select(SenderHealthCheck).where(
            SenderHealthCheck.tenant_id == tenant_id, SenderHealthCheck.sender_id == sender.id
        )
    ).all()
    # The "started" row (status CHECKING) plus the recorded failure row.
    assert len(failed) == 2
    recorded = [c for c in failed if c.error_code == "HEALTH_CHECK_RUN_FAILED"]
    assert len(recorded) == 1
    assert recorded[0].overall_status == "UNKNOWN"
    session.refresh(sender)
    assert sender.health_status == "UNKNOWN"


def test_history_and_latest_read_paths(engine_session) -> None:
    session, tenant_id = engine_session
    sender = _seed_sender(session, tenant_id)
    service = SenderHealthService(session, tenant_id, resolver=HEALTHY_DNS)
    service.run_health_check(sender.id, triggered_by="MANUAL")

    history = service.list_history(sender.id, page=1, page_size=10)
    assert history["total"] == 1
    assert history["total_pages"] == 1
    item = history["items"][0]
    assert item["result_count"] == 9
    assert item["overall_status"] == "HEALTHY"

    latest = service.latest_health(sender.id)
    assert latest is not None
    assert latest["overall_status"] == "HEALTHY"
    assert len(latest["results"]) == 9
    assert latest["results"][0]["check_type"] == "PROVIDER_CONNECTION"

    assert service.latest_health(uuid4()) is None