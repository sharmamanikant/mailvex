from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Base,
    Campaign,
    CampaignRecipient,
    ComplianceResult,
    Contact,
    EmailAccount,
    Suppression,
    Template,
    TemplateVariable,
    TemplateVersion,
    Tenant,
    Unsubscribe,
)
from app.services.compliance import ComplianceService


def _base_campaign(session: Session, tenant: Tenant, *, contact_status: str = "VALID", template_vars: list[str] | None = None, campaign_status: str = "APPROVED") -> dict:
    sender = EmailAccount(
        tenant_id=tenant.id, provider="SMTP", email="sender@example.com", status="CONNECTED"
    )
    contact = Contact(
        tenant_id=tenant.id,
        email="recipient@example.com",
        first_name="Recipient",
        company="Acme",
        validation_status=contact_status,
    )
    template = Template(tenant_id=tenant.id, name="Engine template")
    session.add_all([sender, contact, template])
    session.flush()
    version = TemplateVersion(
        tenant_id=tenant.id,
        template_id=template.id,
        version_number=1,
        subject_template="Hello {{first_name}}",
        html_body="<p>Hello {{first_name}}</p>",
        variable_manifest=template_vars or ["first_name"],
        status="ACTIVE",
    )
    session.add(version)
    session.flush()
    campaign = Campaign(
        tenant_id=tenant.id,
        name="Engine campaign",
        objective="Test",
        sender_id=sender.id,
        template_version_id=version.id,
        status=campaign_status,
        schedule_config={},
    )
    session.add(campaign)
    session.flush()
    link = CampaignRecipient(tenant_id=tenant.id, campaign_id=campaign.id, contact_id=contact.id)
    session.add(link)
    session.commit()
    return {"tenant": tenant, "sender": sender, "contact": contact, "campaign": campaign, "link": link, "version": version}


@pytest.fixture()
def engine_fixture(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'compliance.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Engine Tenant", slug=f"engine-{uuid4().hex[:8]}")
        session.add(tenant)
        session.commit()
        yield session, tenant.id
    engine.dispose()


def test_unsubscribed_recipient_blocks(engine_fixture) -> None:
    session, tenant_id = engine_fixture
    tenant = session.get(Tenant, tenant_id)
    data = _base_campaign(session, tenant)
    session.add(
        Unsubscribe(
            tenant_id=tenant_id,
            email=data["contact"].email,
            token_hash=uuid4().hex,
            unsubscribed_at=datetime.now(UTC),
        )
    )
    session.commit()
    result = ComplianceService(session, tenant_id).evaluate(
        data["campaign"], data["sender"], data["contact"], data["link"]
    )
    assert result.outcome == "BLOCK"
    assert "unsubscribe" in {check.name for check in result.failures}


def test_hard_bounced_recipient_blocks(engine_fixture) -> None:
    session, tenant_id = engine_fixture
    tenant = session.get(Tenant, tenant_id)
    data = _base_campaign(session, tenant)
    session.add(
        Suppression(
            tenant_id=tenant_id,
            email=data["contact"].email,
            reason="HARD_BOUNCE",
            source="test",
            effective_at=datetime.now(UTC),
        )
    )
    session.commit()
    result = ComplianceService(session, tenant_id).evaluate(
        data["campaign"], data["sender"], data["contact"], data["link"]
    )
    assert result.outcome == "BLOCK"
    assert "bounce_status" in {check.name for check in result.failures}


def test_complaint_recipient_blocks(engine_fixture) -> None:
    session, tenant_id = engine_fixture
    tenant = session.get(Tenant, tenant_id)
    data = _base_campaign(session, tenant)
    session.add(
        Suppression(
            tenant_id=tenant_id,
            email=data["contact"].email,
            reason="COMPLAINT",
            source="test",
            effective_at=datetime.now(UTC),
        )
    )
    session.commit()
    result = ComplianceService(session, tenant_id).evaluate(
        data["campaign"], data["sender"], data["contact"], data["link"]
    )
    assert result.outcome == "BLOCK"
    assert "complaint_status" in {check.name for check in result.failures}


def test_unapproved_campaign_blocks(engine_fixture) -> None:
    session, tenant_id = engine_fixture
    tenant = session.get(Tenant, tenant_id)
    data = _base_campaign(session, tenant, campaign_status="DRAFT")
    result = ComplianceService(session, tenant_id).evaluate(
        data["campaign"], data["sender"], data["contact"], data["link"]
    )
    assert result.outcome == "BLOCK"
    assert "campaign_approved" in {check.name for check in result.failures}


def test_disconnected_sender_blocks(engine_fixture) -> None:
    session, tenant_id = engine_fixture
    tenant = session.get(Tenant, tenant_id)
    data = _base_campaign(session, tenant)
    role = session.get(EmailAccount, data["sender"].id)
    role.status = "DISABLED"
    session.commit()
    result = ComplianceService(session, tenant_id).evaluate(
        data["campaign"], role, data["contact"], data["link"]
    )
    assert result.outcome == "BLOCK"
    assert {"sender_connected", "sender_health", "provider_capacity"} <= {
        check.name for check in result.failures
    }


def test_invalid_recipient_blocks(engine_fixture) -> None:
    session, tenant_id = engine_fixture
    tenant = session.get(Tenant, tenant_id)
    data = _base_campaign(session, tenant, contact_status="INVALID")
    result = ComplianceService(session, tenant_id).evaluate(
        data["campaign"], data["sender"], data["contact"], data["link"]
    )
    assert result.outcome == "BLOCK"
    assert "recipient_valid" in {check.name for check in result.failures}


def test_missing_optional_field_warns(engine_fixture) -> None:
    session, tenant_id = engine_fixture
    tenant = session.get(Tenant, tenant_id)
    data = _base_campaign(session, tenant, template_vars=["first_name", "company"])
    contact = session.get(Contact, data["contact"].id)
    contact.company = None
    session.commit()
    result = ComplianceService(session, tenant_id).evaluate(
        data["campaign"], data["sender"], contact, data["link"]
    )
    assert result.outcome == "WARNING"
    personalization = next(check for check in result.checks if check.name == "personalization_data")
    assert personalization.outcome == "WARNING"
    assert "company" in personalization.message


def test_required_personalization_missing_blocks(engine_fixture) -> None:
    session, tenant_id = engine_fixture
    tenant = session.get(Tenant, tenant_id)
    data = _base_campaign(session, tenant, template_vars=["company"])
    template_version = session.get(TemplateVersion, data["version"].id)
    session.add(
        TemplateVariable(
            tenant_id=tenant_id,
            template_version_id=template_version.id,
            name="company",
            required=True,
        )
    )
    contact = session.get(Contact, data["contact"].id)
    contact.company = None
    session.commit()
    result = ComplianceService(session, tenant_id).evaluate(
        data["campaign"], data["sender"], contact, data["link"]
    )
    assert result.outcome == "WARNING"
    personalization = next(check for check in result.checks if check.name == "personalization_data")
    assert personalization.outcome == "WARNING"


def test_valid_message_passes(engine_fixture) -> None:
    session, tenant_id = engine_fixture
    tenant = session.get(Tenant, tenant_id)
    data = _base_campaign(session, tenant)
    result = ComplianceService(session, tenant_id).evaluate(
        data["campaign"], data["sender"], data["contact"], data["link"]
    )
    assert result.outcome == "PASS"
    assert not result.failures


def test_tenant_isolation_suppression(engine_fixture) -> None:
    session, tenant_a_id = engine_fixture
    tenant_a = session.get(Tenant, tenant_a_id)
    tenant_b = Tenant(name="Engine Tenant B", slug=f"engine-b-{uuid4().hex[:8]}")
    session.add(tenant_b)
    session.commit()
    data_a = _base_campaign(session, tenant_a)
    data_b = _base_campaign(session, tenant_b)

    session.add(
        Suppression(
            tenant_id=tenant_a_id,
            email=data_a["contact"].email,
            reason="MANUAL_BLOCK",
            source="test",
            effective_at=datetime.now(UTC),
        )
    )
    session.commit()

    result_a = ComplianceService(session, tenant_a_id).evaluate(
        data_a["campaign"], data_a["sender"], data_a["contact"], data_a["link"]
    )
    result_b = ComplianceService(session, tenant_b.id).evaluate(
        data_b["campaign"], data_b["sender"], data_b["contact"], data_b["link"]
    )
    assert result_a.outcome == "BLOCK"
    assert result_b.outcome == "PASS"


def test_results_persisted_and_audited(engine_fixture) -> None:
    session, tenant_id = engine_fixture
    tenant = session.get(Tenant, tenant_id)
    data = _base_campaign(session, tenant)
    session.add(
        Unsubscribe(
            tenant_id=tenant_id,
            email=data["contact"].email,
            token_hash=uuid4().hex,
            unsubscribed_at=datetime.now(UTC),
        )
    )
    session.commit()
    ComplianceService(session, tenant_id).evaluate(
        data["campaign"], data["sender"], data["contact"], data["link"]
    )
    session.commit()

    records = session.scalars(
        select(ComplianceResult).where(ComplianceResult.tenant_id == tenant_id)
    ).all()
    check_types = {record.check_type for record in records}
    assert "unsubscribe" in check_types
    assert any(record.result == "BLOCK" for record in records)

    audit_actions = {
        log.action
        for log in session.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant_id)).all()
    }
    assert "COMPLIANCE_BLOCK" in audit_actions


def test_warning_audited(engine_fixture) -> None:
    session, tenant_id = engine_fixture
    tenant = session.get(Tenant, tenant_id)
    data = _base_campaign(session, tenant, template_vars=["first_name", "company"])
    contact = session.get(Contact, data["contact"].id)
    contact.company = None
    session.commit()
    ComplianceService(session, tenant_id).evaluate(
        data["campaign"], data["sender"], contact, data["link"]
    )
    session.commit()
    audit_actions = {
        log.action
        for log in session.scalars(select(AuditLog).where(AuditLog.tenant_id == tenant_id)).all()
    }
    assert "COMPLIANCE_WARNING" in audit_actions


def test_policy_max_campaign_size_blocks(engine_fixture) -> None:
    session, tenant_id = engine_fixture
    tenant = session.get(Tenant, tenant_id)
    data = _base_campaign(session, tenant)
    second = Contact(
        tenant_id=tenant_id,
        email="second@example.com",
        first_name="Second",
        validation_status="VALID",
    )
    session.add(second)
    session.flush()
    campaign = session.get(Campaign, data["campaign"].id)
    if not any(item.contact_id == second.id for item in campaign.recipients):
        campaign.recipients.append(
            CampaignRecipient(tenant_id=tenant_id, campaign_id=campaign.id, contact_id=second.id)
        )
    config = dict(campaign.schedule_config)
    config["policy"] = {"maximum_campaign_size": 1}
    campaign.schedule_config = config
    session.commit()
    result = ComplianceService(session, tenant_id).evaluate(
        campaign, data["sender"], data["contact"], data["link"]
    )
    assert result.outcome == "BLOCK"
    assert "campaign_policy" in {check.name for check in result.failures}


def test_policy_tenant_require_approval_blocks(engine_fixture) -> None:
    session, tenant_id = engine_fixture
    tenant = session.get(Tenant, tenant_id)
    data = _base_campaign(session, tenant, campaign_status="DRAFT")
    campaign = session.get(Campaign, data["campaign"].id)
    config = dict(campaign.schedule_config)
    config["policy"] = {"require_approval": True}
    campaign.schedule_config = config
    session.commit()
    result = ComplianceService(session, tenant_id).evaluate(
        campaign, data["sender"], data["contact"], data["link"]
    )
    assert result.outcome == "BLOCK"
    assert "tenant_policy" in {check.name for check in result.failures}
