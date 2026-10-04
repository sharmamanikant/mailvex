"""Phase 10Q campaign sender pools (+ System B delivery routing) and reply sync."""

from __future__ import annotations

import types
from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.email_providers.base import EmailProviderError, ProviderErrorCode
from app.models import (
    Base,
    Campaign,
    CampaignRecipient,
    CampaignSender,
    Contact,
    DeliveryJob,
    EmailAccount,
    Message,
    SenderAccount,
    SenderConnection,
    Tenant,
)
from app.security.rate_limit import RateLimitResult
from app.services.delivery_jobs import DeliveryJobError, DeliveryJobService
from app.services.integrations import (
    IntegrationConflictError,
    IntegrationValidationError,
)
from app.services.reply_sync import ReplySyncService
from app.services.sender_health import SenderHealthService
from app.services.sender_pool import SenderPoolService
from app.services.sending import SenderThrottledError, SendingService


@pytest.fixture()
def pool_db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'pool.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Pool Tenant", slug=f"pool-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        legacy = EmailAccount(tenant_id=tenant.id, provider="SMTP", email="legacy@example.com", status="CONNECTED")
        session.add(legacy)
        session.flush()
        campaign = Campaign(
            tenant_id=tenant.id,
            name="Pool campaign",
            objective="Test",
            sender_id=legacy.id,
            status="APPROVED",
            schedule_config={
                "start_at": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
                "max_attempts": 2,
            },
        )
        session.add(campaign)
        session.flush()
        connection = SenderConnection(
            tenant_id=tenant.id,
            provider="SENDGRID",
            connection_type="CREDENTIAL",
            status="CONNECTED",
        )
        session.add(connection)
        session.flush()

        def sender(email: str, health: str = "HEALTHY") -> SenderAccount:
            account = SenderAccount(
                tenant_id=tenant.id,
                connection_id=connection.id,
                provider="SENDGRID",
                email=email,
                status="ACTIVE",
                health_status=health,
                campaign_enabled=True,
            )
            session.add(account)
            session.flush()
            return account

        healthy_a = sender("a@example.com")
        healthy_b = sender("b@example.com")
        session.commit()
        yield session, tenant.id, campaign.id, connection.id, healthy_a, healthy_b, legacy.id
    engine.dispose()


def _service(session: Session, tenant_id) -> SenderPoolService:
    return SenderPoolService(session, tenant_id)


# ------------------------------------------------------------------ #
# Pool management + validation
# ------------------------------------------------------------------ #
def test_add_sender_validates_campaign_opt_in(pool_db) -> None:
    session, tenant_id, campaign_id, connection_id, *_ = pool_db
    session.add(SenderAccount(tenant_id=tenant_id, connection_id=connection_id, provider="SENDGRID", email="nope@example.com", status="ACTIVE", campaign_enabled=False))
    session.commit()
    other = session.query(SenderAccount).filter_by(email="nope@example.com").one()
    with pytest.raises(IntegrationValidationError):
        _service(session, tenant_id).add_sender(campaign_id, other.id)


def test_add_sender_rejects_unhealthy(pool_db) -> None:
    session, tenant_id, campaign_id, connection_id, *_ = pool_db
    session.add(SenderAccount(tenant_id=tenant_id, connection_id=connection_id, provider="SENDGRID", email="sick@example.com", status="ACTIVE", health_status="FAILED", campaign_enabled=True))
    session.commit()
    sick = session.query(SenderAccount).filter_by(email="sick@example.com").one()
    with pytest.raises(IntegrationValidationError):
        _service(session, tenant_id).add_sender(campaign_id, sick.id)


def test_add_sender_duplicate_conflicts(pool_db) -> None:
    session, tenant_id, campaign_id, _c, healthy_a, *_ = pool_db
    _service(session, tenant_id).add_sender(campaign_id, healthy_a.id)
    with pytest.raises(IntegrationConflictError):
        _service(session, tenant_id).add_sender(campaign_id, healthy_a.id)


def test_pool_lifecycle(pool_db) -> None:
    session, tenant_id, campaign_id, _c, healthy_a, healthy_b, _ = pool_db
    service = _service(session, tenant_id)
    service.add_sender(campaign_id, healthy_a.id, daily_limit=10, enabled=True)
    service.add_sender(campaign_id, healthy_b.id)
    assert len(service.list_pool(campaign_id)) == 2
    service.set_daily_limit(campaign_id, healthy_a.id, 25)
    service.set_enabled(campaign_id, healthy_b.id, False)
    session.expire_all()
    updated = service.list_pool(campaign_id)
    a = next(item for item in updated if item.sender_id == healthy_a.id)
    assert a.daily_limit == 25
    assert not next(item for item in updated if item.sender_id == healthy_b.id).enabled
    service.remove_sender(campaign_id, healthy_b.id)
    assert len(service.list_pool(campaign_id)) == 1


def test_rotation_excludes_disabled_members(pool_db) -> None:
    session, tenant_id, campaign_id, _c, healthy_a, healthy_b, _ = pool_db
    service = _service(session, tenant_id)
    service.add_sender(campaign_id, healthy_a.id)
    service.add_sender(campaign_id, healthy_b.id)
    service.set_enabled(campaign_id, healthy_b.id, False)
    rotation = service.rotation(campaign_id)
    assert [acc.email for acc in rotation] == [healthy_a.email]


def test_rotation_excludes_senders_that_degraded_since_admission(pool_db) -> None:
    session, tenant_id, campaign_id, connection_id, *_ = pool_db
    session.add(SenderAccount(tenant_id=tenant_id, connection_id=connection_id, provider="SENDGRID", email="sick@example.com", status="ACTIVE", health_status="HEALTHY", campaign_enabled=True))
    session.flush()
    sick = session.query(SenderAccount).filter_by(email="sick@example.com").one()
    healthy_a = session.query(SenderAccount).filter_by(email="a@example.com").one()
    _service(session, tenant_id).add_sender(campaign_id, healthy_a.id)
    _service(session, tenant_id).add_sender(campaign_id, sick.id)
    session.query(SenderAccount).filter_by(id=sick.id).update({"health_status": "FAILED"})
    session.commit()
    assert [a.email for a in _service(session, tenant_id).rotation(campaign_id)] == ["a@example.com"]


# ------------------------------------------------------------------ #
# Materialization: System B pool routing vs legacy single sender
# ------------------------------------------------------------------ #
def test_materialize_assigns_pool_senders_round_robin(pool_db) -> None:
    session, tenant_id, campaign_id, _c, healthy_a, healthy_b, legacy_id = pool_db
    contact_a = Contact(tenant_id=tenant_id, email="r1@example.com")
    contact_b = Contact(tenant_id=tenant_id, email="r2@example.com")
    session.add_all([contact_a, contact_b])
    session.flush()
    session.add_all([
        CampaignRecipient(tenant_id=tenant_id, campaign_id=campaign_id, contact_id=contact_a.id),
        CampaignRecipient(tenant_id=tenant_id, campaign_id=campaign_id, contact_id=contact_b.id),
    ])
    _service(session, tenant_id).add_sender(campaign_id, healthy_a.id)
    _service(session, tenant_id).add_sender(campaign_id, healthy_b.id)
    session.commit()

    jobs = DeliveryJobService(session, tenant_id).materialize_for_campaign(campaign_id)
    assigned = [job.sender_account_id for job in jobs]
    assert len(assigned) == 2
    assert set(assigned) == {healthy_a.id, healthy_b.id}
    assert all(job.sender_id == legacy_id for job in jobs)


def test_materialize_rejects_pool_with_no_usable_senders(pool_db) -> None:
    session, tenant_id, campaign_id, _c, healthy_a, _b, _ = pool_db
    contact = Contact(tenant_id=tenant_id, email="r@example.com")
    session.add(contact)
    session.flush()
    session.add(CampaignRecipient(tenant_id=tenant_id, campaign_id=campaign_id, contact_id=contact.id))
    session.commit()
    _service(session, tenant_id).add_sender(campaign_id, healthy_a.id)
    session.commit()
    session.query(CampaignSender).update({"enabled": False})
    session.query(SenderAccount).update({"health_status": "FAILED"})
    session.commit()
    with pytest.raises(DeliveryJobError, match="no usable senders"):
        DeliveryJobService(session, tenant_id).materialize_for_campaign(campaign_id)


def test_materialize_legacy_path_unchanged(pool_db) -> None:
    session, tenant_id, campaign_id, _c, _a, _b, legacy_id = pool_db
    contact = Contact(tenant_id=tenant_id, email="r@example.com")
    session.add(contact)
    session.flush()
    session.add(CampaignRecipient(tenant_id=tenant_id, campaign_id=campaign_id, contact_id=contact.id))
    session.commit()
    jobs = DeliveryJobService(session, tenant_id).materialize_for_campaign(campaign_id)
    assert len(jobs) == 1
    assert jobs[0].sender_id == legacy_id
    assert jobs[0].sender_account_id is None


# ------------------------------------------------------------------ #
# System B delivery routing
# ------------------------------------------------------------------ #
class _FakeLimiter:
    def check_limit(self, _key: str, _limit: int, _window: int) -> RateLimitResult:
        return RateLimitResult(True, 0)


class _FakeSendProvider:
    def __init__(self, message_id: str = "sysb-pm-1", error: EmailProviderError | None = None) -> None:
        self.message_id = message_id
        self.error = error
        self.last_message = None

    def send_message(self, _config, message) -> str:
        if self.error is not None:
            raise self.error
        self.last_message = message
        return self.message_id


def test_system_b_send_creates_message_and_health(pool_db, monkeypatch) -> None:
    session, tenant_id, campaign_id, _c, healthy_a, _b, legacy_id = pool_db
    campaign = session.get(Campaign, campaign_id)

    class _RLS:
        def __init__(self, _url: str, fail_open: bool = False) -> None:
            self.limiter = _FakeLimiter()

        def check_limit(self, key: str, limit: int, window: int) -> RateLimitResult:
            return self.limiter.check_limit(key, limit, window)

    monkeypatch.setattr("app.services.sender_quotas.RateLimitService", _RLS)
    provider = _FakeSendProvider()
    import app.services.sending as sending

    monkeypatch.setattr(sending, "get_provider", lambda _name: provider)
    contact = Contact(tenant_id=tenant_id, email="recipient@example.com")
    session.add(contact)
    session.flush()
    legacy = session.get(EmailAccount, legacy_id)
    job = DeliveryJob(
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        recipient_id=contact.id,
        sender_id=legacy_id,
        sender_account_id=healthy_a.id,
        scheduled_at=datetime.now(UTC),
        status="PROCESSING",
    )
    session.add(job)
    session.commit()
    rendered = types.SimpleNamespace(subject="Hi", text_body="text", html_body="<p>html</p>")
    message = SendingService(session, tenant_id)._send_delivery_job_system_b(
        job, campaign, contact, legacy, None, rendered
    )
    assert message.provider_message_id == "sysb-pm-1"
    assert provider.last_message.to == ("recipient@example.com",)
    assert message.subject == "Hi"
    health = SenderHealthService(session, tenant_id).refresh(healthy_a.id)
    assert health.health_status == "HEALTHY"
    assert health.last_success_at is not None


def test_system_b_send_maps_rate_limit_to_throttle(pool_db, monkeypatch) -> None:
    session, tenant_id, campaign_id, _c, healthy_a, _b, legacy_id = pool_db

    class _RLS:
        def __init__(self, _url: str, fail_open: bool = False) -> None:
            pass

        def check_limit(self, _key: str, _limit: int, _window: int) -> RateLimitResult:
            return RateLimitResult(True, 0)

    monkeypatch.setattr("app.services.sender_quotas.RateLimitService", _RLS)
    provider = _FakeSendProvider(error=EmailProviderError(ProviderErrorCode.RATE_LIMITED, "slow down", retry_after=120))
    import app.services.sending as sending

    monkeypatch.setattr(sending, "get_provider", lambda _name: provider)
    campaign = session.get(Campaign, campaign_id)
    contact = Contact(tenant_id=tenant_id, email="recipient@example.com")
    session.add(contact)
    session.flush()
    job = DeliveryJob(
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        recipient_id=contact.id,
        sender_id=legacy_id,
        sender_account_id=healthy_a.id,
        scheduled_at=datetime.now(UTC),
        status="PROCESSING",
    )
    session.add(job)
    session.commit()
    legacy = session.get(EmailAccount, legacy_id)
    rendered = types.SimpleNamespace(subject="Hi", text_body="t", html_body="<p>h</p>")
    with pytest.raises(SenderThrottledError) as excinfo:
        SendingService(session, tenant_id)._send_delivery_job_system_b(
            job, campaign, contact, legacy, None, rendered
        )
    assert excinfo.value.retry_after == 120


# ------------------------------------------------------------------ #
# Reply sync
# ------------------------------------------------------------------ #
def test_reply_sync_links_in_reply_to(pool_db) -> None:
    session, tenant_id, campaign_id, _conn, healthy_a, _b, legacy_id = pool_db
    contact = Contact(tenant_id=tenant_id, email="recipient@example.com")
    session.add(contact)
    session.flush()
    original = Message(
        tenant_id=tenant_id,
        campaign_id=campaign_id,
        sender_id=legacy_id,
        contact_id=contact.id,
        provider_message_id="orig-pm-1",
        subject="Campaign",
        status="SENT",
    )
    session.add(original)
    session.commit()
    session.query(SenderAccount).filter_by(id=healthy_a.id).update({"reply_sync_enabled": True})

    import app.services.reply_sync as rs

    class _InboxProvider:
        def get_capabilities(self):
            return types.SimpleNamespace(supports_inbox_sync=True)

        def sync_inbox(self, _config, limit: int = 50) -> list[dict[str, object]]:
            return [{
                "message_id": "reply-1",
                "in_reply_to": "orig-pm-1",
                "subject": "Re: Campaign",
                "from": "recipient@example.com",
            }]

    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(rs, "get_provider", lambda _name: _InboxProvider())
    count = ReplySyncService(session, tenant_id).sync_sender(healthy_a.id)
    monkeypatch.undo()
    assert count == 1
    session.expire_all()
    original = session.get(Message, original.id)
    assert any(event.event_type == "REPLY" for event in original.events)


def test_reply_sync_skips_when_disabled(pool_db, monkeypatch) -> None:
    session, tenant_id, _c, _conn, healthy_a, _b, _legacy = pool_db
    count = ReplySyncService(session, tenant_id).sync_sender(healthy_a.id)
    assert count == 0


def test_reply_sync_requires_inbox_support(pool_db, monkeypatch) -> None:
    session, tenant_id, _c, _conn, healthy_a, _b, _legacy = pool_db
    session.query(SenderAccount).filter_by(id=healthy_a.id).update({"reply_sync_enabled": True})
    session.commit()
    import app.services.reply_sync as rs

    class _NoInboxProvider:
        def get_capabilities(self):
            return types.SimpleNamespace(supports_inbox_sync=False)

        def sync_inbox(self, _config, limit: int = 50) -> list[dict[str, object]]:
            raise AssertionError("should not be called")

    monkeypatch.setattr(rs, "get_provider", lambda _name: _NoInboxProvider())
    assert ReplySyncService(session, tenant_id).sync_sender(healthy_a.id) == 0