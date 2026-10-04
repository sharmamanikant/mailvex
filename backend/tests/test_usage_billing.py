from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.billing import (
    EVENT_AI_GENERATION,
    EVENT_CAMPAIGN_CREATED,
    EVENT_CONTACT_CREATED,
    EVENT_MESSAGE_SENT,
    EVENT_STORAGE_USED,
    EVENT_TEAM_MEMBER_ADDED,
    UsageLimitError,
    UsageService,
)
from app.models import (
    AuditLog,
    Base,
    Contact,
    Tenant,
    TenantSubscription,
    UsageEvent,
    UsageRecord,
    User,
)
from app.schemas.contacts import ContactCreate
from app.security.passwords import hash_password
from app.services.contacts import ContactService


@pytest.fixture()
def usage_env(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'usage.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()

    tenant_a = Tenant(name="Alpha", slug=f"alpha-{uuid4().hex[:8]}")
    tenant_b = Tenant(name="Beta", slug=f"beta-{uuid4().hex[:8]}")
    session.add_all([tenant_a, tenant_b])
    session.flush()
    user_a = User(
        tenant_id=tenant_a.id,
        email="owner@alpha.example.com",
        password_hash=hash_password("correct horse battery staple"),
        display_name="Owner A",
    )
    session.add_all([
        user_a,
        TenantSubscription(
            tenant_id=tenant_a.id,
            plan_code="free",
            status="ACTIVE",
            custom_limits={},
            period_start=datetime.now(UTC),
        ),
        TenantSubscription(
            tenant_id=tenant_b.id,
            plan_code="free",
            status="ACTIVE",
            custom_limits={},
            period_start=datetime.now(UTC),
        ),
    ])
    session.commit()
    session.close()
    yield factory, tenant_a.id, tenant_b.id, user_a.id
    engine.dispose()


def _custom_limit(factory: sessionmaker, tenant_id, **metrics: int) -> None:
    with factory() as session:
        subscription = session.scalar(
            select(TenantSubscription).where(
                TenantSubscription.tenant_id == tenant_id
            )
        )
        subscription.custom_limits = dict(metrics)
        session.commit()


def _event_count(factory: sessionmaker, tenant_id: object, event_type: str | None = None) -> int:
    with factory() as session:
        query = select(func.count(UsageEvent.id)).where(
            UsageEvent.tenant_id == tenant_id
        )
        if event_type is not None:
            query = query.where(UsageEvent.event_type == event_type)
        return int(session.scalar(query) or 0)


def _reserved_total(factory: sessionmaker, tenant_id: object, metric: str) -> Decimal:
    with factory() as session:
        return Decimal(
            str(
                session.scalar(
                    select(func.coalesce(func.sum(UsageRecord.quantity), 0)).where(
                        UsageRecord.tenant_id == tenant_id,
                        UsageRecord.metric == metric,
                    )
                )
                or 0
            )
        )


# ---------------------------------------------------------------- limits

def test_periodic_limit_enforced(usage_env) -> None:
    factory, tenant_a, _, _ = usage_env
    _custom_limit(factory, tenant_a, ai_generations=3)

    with factory() as session:
        service = UsageService(session, tenant_a)
        for _ in range(3):
            service.meter(EVENT_AI_GENERATION, resource_type="ai_generation")
        session.commit()
    with factory() as session:
        service = UsageService(session, tenant_a)
        with pytest.raises(UsageLimitError) as exc_info:
            service.meter(EVENT_AI_GENERATION, resource_type="ai_generation")
        session.rollback()

    assert "AI generations" in str(exc_info.value)
    assert exc_info.value.limit == 3
    assert exc_info.value.used == 3
    assert _event_count(factory, tenant_a, EVENT_AI_GENERATION) == 3
    assert _reserved_total(factory, tenant_a, "ai_generations") == 3


def test_absolute_limit_enforced(usage_env) -> None:
    factory, tenant_a, _, _ = usage_env
    _custom_limit(factory, tenant_a, contacts=2)

    with factory() as session:
        session.add_all([
            Contact(tenant_id=tenant_a, email="existing1@example.com"),
            Contact(tenant_id=tenant_a, email="existing2@example.com"),
        ])
        session.commit()

    with factory() as session:
        service = UsageService(session, tenant_a)
        with pytest.raises(UsageLimitError):
            service.meter(EVENT_CONTACT_CREATED, resource_type="contact")
        session.rollback()

    with factory() as session:
        assert session.scalar(
            select(func.count(Contact.id)).where(Contact.tenant_id == tenant_a)
        ) == 2


def test_limit_reached_clear_message_not_silent(usage_env) -> None:
    factory, tenant_a, _, _ = usage_env
    _custom_limit(factory, tenant_a, campaigns=0)
    with factory() as session:
        with pytest.raises(UsageLimitError) as exc_info:
            UsageService(session, tenant_a).meter(
                EVENT_CAMPAIGN_CREATED, resource_type="campaign"
            )
        session.rollback()
    message = str(exc_info.value)
    assert "Campaign" in message
    assert "Free" in message
    assert "0/0" in message or "reached" in message


# --------------------------------------------------------- concurrency

def test_concurrent_claims_never_overshoot(usage_env) -> None:
    factory, tenant_a, _, _ = usage_env
    _custom_limit(factory, tenant_a, ai_generations=10)

    def claim(_):
        with factory() as session:
            try:
                UsageService(session, tenant_a).meter(
                    EVENT_AI_GENERATION, resource_type="ai_generation"
                )
                session.commit()
                return "ok"
            except UsageLimitError:
                session.rollback()
                return "limited"
            except Exception as exc:  # pragma: no cover - unexpected
                session.rollback()
                return f"error:{exc}"

    with ThreadPoolExecutor(max_workers=12) as pool:
        results = list(pool.map(claim, range(40)))

    assert not any(r.startswith("error:") for r in results), results
    successes = [r for r in results if r == "ok"]
    limited = [r for r in results if r == "limited"]
    assert len(successes) == 10
    assert len(limited) == 30
    assert _event_count(factory, tenant_a, EVENT_AI_GENERATION) == 10
    assert _reserved_total(factory, tenant_a, "ai_generations") == 10


# --------------------------------------------------------- tenant isolation

def test_tenant_isolation(usage_env) -> None:
    factory, tenant_a, tenant_b, _ = usage_env
    _custom_limit(factory, tenant_a, ai_generations=1)

    with factory() as session:
        UsageService(session, tenant_a).meter(EVENT_AI_GENERATION)
        session.commit()

    with factory() as session:
        # tenant B is on the free default plan (200/month) and is unaffected
        # by tenant A's custom limit.
        UsageService(session, tenant_b).meter(EVENT_AI_GENERATION)
        session.commit()

    with factory() as session:
        service = UsageService(session, tenant_a)
        with pytest.raises(UsageLimitError):
            service.enforce("ai_generations", quantity=1)
        session.rollback()

    envelope_a = UsageService(factory(), tenant_a).envelope()
    envelope_b = UsageService(factory(), tenant_b).envelope()
    used_a = next(m for m in envelope_a["metrics"] if m["metric"] == "ai_generations")["used"]
    used_b = next(m for m in envelope_b["metrics"] if m["metric"] == "ai_generations")["used"]
    assert used_a == 1
    assert used_b == 1
    assert _event_count(factory, tenant_a) == 1
    assert _event_count(factory, tenant_b) == 1


# --------------------------------------------------------- plan changes

def test_plan_change_updates_limits_and_resets_period(usage_env) -> None:
    factory, tenant_a, _, actor_id = usage_env
    old_period = datetime.now(UTC)

    with factory() as session:
        service = UsageService(session, tenant_a, actor_id)
        subscription = service.change_plan("business", reset_period=True)
        session.commit()
        assert subscription.plan_code == "business"
        assert subscription.period_start is not None

    with factory() as session:
        subscription = session.scalar(
            select(TenantSubscription).where(
                TenantSubscription.tenant_id == tenant_a
            )
        )
        assert subscription.plan_code == "business"
        assert subscription.period_start is not None
        assert subscription.period_start != old_period
        plan_changed = session.scalar(
            select(AuditLog).where(
                AuditLog.tenant_id == tenant_a,
                AuditLog.action == "PLAN_CHANGED",
            )
        )
        assert plan_changed is not None

    envelope = UsageService(factory(), tenant_a).envelope()
    assert envelope["plan_code"] == "business"
    business_cap = next(
        m for m in envelope["metrics"] if m["metric"] == "ai_generations"
    )["limit"]
    assert business_cap is None or business_cap >= 20000


def test_envelope_reports_usage_calculation(usage_env) -> None:
    factory, tenant_a, _, _ = usage_env
    with factory() as session:
        service = UsageService(session, tenant_a)
        service.meter(EVENT_CONTACT_CREATED, resource_type="contact")
        service.meter(EVENT_CONTACT_CREATED, resource_type="contact")
        service.meter(EVENT_AI_GENERATION)
        service.meter(EVENT_MESSAGE_SENT, resource_type="message")
        service.meter(EVENT_STORAGE_USED, quantity=Decimal("12.5"))
        service.record_event(
            EVENT_TEAM_MEMBER_ADDED, resource_type="user"
        )
        session.commit()

    envelope = UsageService(factory(), tenant_a).envelope()
    by_metric = {m["metric"]: m for m in envelope["metrics"]}
    assert by_metric["contacts"]["used"] == 0  # live contact rows, not events
    assert by_metric["ai_generations"]["used"] == 1
    assert by_metric["messages"]["used"] == 1
    assert by_metric["storage_mb"]["used"] == 12.5
    assert by_metric["team_members"]["used"] == 1  # live user rows
    assert envelope["plan_code"] == "free"


# ------------------------------------------------- messages: track, don't gate

def test_message_events_tracked_without_gating(usage_env) -> None:
    factory, tenant_a, _, _ = usage_env
    _custom_limit(factory, tenant_a, messages=2)

    with factory() as session:
        service = UsageService(session, tenant_a)
        # The sending path only records; it never hard-gates on the message
        # quota (compliance/provider throttling remain the priority gates).
        for _ in range(5):
            service.record_event(EVENT_MESSAGE_SENT, resource_type="message")
        session.commit()
        assert _event_count(factory, tenant_a, EVENT_MESSAGE_SENT) == 5

    with factory() as session:
        service = UsageService(session, tenant_a)
        # Recording 5 events never consumes reservation headroom and the
        # message quota is never hard-gated (sending keeps the delivery paths).
        service.enforce("messages", quantity=1)
        session.rollback()

    envelope = UsageService(factory(), tenant_a).envelope()
    messages = next(m for m in envelope["metrics"] if m["metric"] == "messages")
    assert messages["used"] == 5


# ----------------------------------------------------- import headroom

def test_import_contact_headroom_enforced(usage_env) -> None:
    factory, tenant_a, _, _ = usage_env
    _custom_limit(factory, tenant_a, contacts=2)

    from app.services.imports import ImportService

    with factory() as session:
        session.add(Contact(tenant_id=tenant_a, email="existing@example.com"))
        session.commit()

    with factory() as session:
        service = ImportService(session, tenant_a)
        service.enforce_contact_headroom(1)
        with pytest.raises(UsageLimitError):
            service.enforce_contact_headroom(2)
        session.rollback()


# ---------------------------------------------- service-path integration

def test_contact_service_create_enforces_plan(usage_env) -> None:
    factory, tenant_a, _, actor_id = usage_env
    _custom_limit(factory, tenant_a, contacts=1)

    with factory() as session:
        ContactService(session, tenant_a, actor_id).create(
            ContactCreate(email="one@example.com")
        )
    with factory() as session:
        with pytest.raises(UsageLimitError):
            ContactService(session, tenant_a, actor_id).create(
                ContactCreate(email="two@example.com")
            )
        session.rollback()

    with factory() as session:
        assert session.scalar(
            select(func.count(Contact.id)).where(Contact.tenant_id == tenant_a)
        ) == 1
        assert _event_count(factory, tenant_a, EVENT_CONTACT_CREATED) == 1