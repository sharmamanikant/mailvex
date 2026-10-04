from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    Campaign,
    CampaignRecipient,
    Contact,
    EmailAccount,
    SuppressionEntry,
    Tenant,
)
from app.services.compliance import ComplianceService
from app.services.suppression_engine import (
    PROTECTED_TYPES,
    SUPPRESSION_TYPES,
    SuppressionEngine,
    SuppressionEngineError,
)


@pytest.fixture()
def engine_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'suppression_engine.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    engine.dispose()


@pytest.fixture()
def org_fixture(engine_fixture):
    session = engine_fixture
    tenant = Tenant(name="Suppression Engine Tenant", slug=f"se-{uuid4().hex[:8]}")
    session.add(tenant)
    session.flush()
    sender = EmailAccount(tenant_id=tenant.id, provider="SMTP", email="sender@example.com", status="CONNECTED")
    contact = Contact(tenant_id=tenant.id, email="Recipient@Example.com", validation_status="VALID")
    session.add_all([sender, contact])
    session.flush()
    campaign = Campaign(tenant_id=tenant.id, name="Engine campaign", objective="Test", sender_id=sender.id, status="APPROVED", schedule_config={})
    session.add(campaign)
    session.flush()
    recipient_link = CampaignRecipient(tenant_id=tenant.id, campaign_id=campaign.id, contact_id=contact.id)
    session.add(recipient_link)
    session.commit()
    return session, tenant.id, sender, contact, campaign, recipient_link


def test_manual_suppression_blocks_future_sends(org_fixture) -> None:
    session, tenant_id, sender, contact, campaign, recipient_link = org_fixture
    engine = SuppressionEngine(session, tenant_id)
    engine.suppress(contact.email, "MANUAL", "operator", reason="Invalid address")
    session.commit()
    assert engine.is_suppressed(contact.email)
    result = ComplianceService(session, tenant_id).check_recipient(campaign, sender, contact, recipient_link)
    assert any(check.name == "suppression" and check.outcome == "BLOCK" for check in result.failures)
    assert recipient_link.eligibility_status == "SUPPRESSED"


def test_cross_tenant_isolation(engine_fixture) -> None:
    session = engine_fixture
    tenant_a = Tenant(name="A", slug=f"a-{uuid4().hex[:8]}")
    tenant_b = Tenant(name="B", slug=f"b-{uuid4().hex[:8]}")
    session.add_all([tenant_a, tenant_b])
    session.flush()
    SuppressionEngine(session, tenant_a.id).suppress("shared@example.com", "MANUAL", "operator")
    session.commit()
    assert SuppressionEngine(session, tenant_a.id).is_suppressed("shared@example.com")
    assert not SuppressionEngine(session, tenant_b.id).is_suppressed("shared@example.com")


def test_suppress_normalizes_and_upserts(engine_fixture) -> None:
    session = engine_fixture
    tenant = Tenant(name="Norm", slug=f"norm-{uuid4().hex[:8]}")
    session.add(tenant)
    session.commit()
    engine = SuppressionEngine(session, tenant.id)
    first = engine.suppress(" Foo@Example.COM ", "MANUAL", "test")
    session.commit()
    second = engine.suppress("foo@example.com", "ADMIN_BLOCKED", "test", reason="changed")
    session.commit()
    assert first.id == second.id
    assert session.query(SuppressionEntry).count() == 1
    entry = session.query(SuppressionEntry).one()
    assert entry.email_normalized == "foo@example.com"
    assert entry.type == "ADMIN_BLOCKED"
    assert entry.active is True


def test_provider_derived_removal_is_denied(engine_fixture) -> None:
    session = engine_fixture
    tenant = Tenant(name="Protected", slug=f"prot-{uuid4().hex[:8]}")
    session.add(tenant)
    session.flush()
    engine = SuppressionEngine(session, tenant.id)
    for entry_type in sorted(PROTECTED_TYPES):
        entry = engine.suppress(f"{entry_type.lower()}@example.com", entry_type, "provider-event", provider="GOOGLE")
        session.commit()
        with pytest.raises(SuppressionEngineError):
            engine.remove(entry.id)
        session.rollback()
    # Manual entries can be removed without force.
    manual = engine.suppress("manual@example.com", "MANUAL", "operator")
    session.commit()
    engine.remove(manual.id)
    session.commit()
    assert not engine.is_suppressed("manual@example.com")


def test_force_removal_clears_contact_marker(org_fixture) -> None:
    session, tenant_id, _sender, contact, _campaign, _link = org_fixture
    engine = SuppressionEngine(session, tenant_id)
    entry = engine.suppress(contact.email, "HARD_BOUNCE", "provider-event", provider="GOOGLE", contact_id=contact.id)
    session.commit()
    contact.suppression_status = "SUPPRESSED"
    session.commit()
    engine.remove(entry.id, force=True)
    session.commit()
    assert not engine.is_suppressed(contact.email)
    assert not engine.is_suppressed(contact.email.upper())


def test_invalid_type_is_rejected(engine_fixture) -> None:
    session = engine_fixture
    tenant = Tenant(name="Invalid", slug=f"inv-{uuid4().hex[:8]}")
    session.add(tenant)
    session.commit()
    with pytest.raises(SuppressionEngineError):
        SuppressionEngine(session, tenant.id).suppress("x@example.com", "NOT_A_TYPE", "test")


def test_list_filters_type_search_and_inactive(engine_fixture) -> None:
    session = engine_fixture
    tenant = Tenant(name="List", slug=f"list-{uuid4().hex[:8]}")
    session.add(tenant)
    session.commit()
    engine = SuppressionEngine(session, tenant.id)
    engine.suppress("a@example.com", "MANUAL", "operator", reason="spam signup")
    engine.suppress("b@example.com", "ADMIN_BLOCKED", "operator", reason="VIP toxic")
    session.commit()
    assert len(engine.list_entries(entry_type="MANUAL")) == 1
    assert len(engine.list_entries(search="vip")) == 1
    assert len(engine.list_entries(search="VIP")) == 1
    entry = engine.list_entries(entry_type="MANUAL")[0]
    engine.remove(entry.id)
    session.commit()
    assert len(engine.list_entries()) == 1
    assert len(engine.list_entries(include_inactive=True)) == 2


def test_legacy_suppression_also_blocks(org_fixture) -> None:
    from datetime import UTC, datetime

    from app.models import Suppression

    session, tenant_id, sender, contact, campaign, recipient_link = org_fixture
    session.add(Suppression(tenant_id=tenant_id, email=contact.email.lower(), reason="MANUAL_BLOCK", source="legacy", effective_at=datetime.now(UTC)))
    session.commit()
    result = ComplianceService(session, tenant_id).check_recipient(campaign, sender, contact, recipient_link)
    assert any(check.name == "suppression" and check.outcome == "BLOCK" for check in result.failures)


def test_suppression_types_exposed(engine_fixture) -> None:
    session = engine_fixture
    tenant = Tenant(name="Types", slug=f"types-{uuid4().hex[:8]}")
    session.add(tenant)
    session.commit()
    types = SuppressionEngine(session, tenant.id).list_types()
    assert set(types) == set(SUPPRESSION_TYPES)
