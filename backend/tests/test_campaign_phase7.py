from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Base,
    Contact,
    ContactList,
    ContactListMember,
    ContactSegment,
    EmailAccount,
    Suppression,
    Template,
    TemplateVersion,
    Tenant,
)
from app.schemas.campaigns import CampaignCreate, CampaignUpdate
from app.services.campaigns import CampaignError, CampaignService


@pytest.fixture()
def campaign_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'phase7.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Phase7 Tenant", slug=f"phase7-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        sender = EmailAccount(tenant_id=tenant.id, provider="SMTP", email="sender@example.com", status="CONNECTED")
        contact = Contact(tenant_id=tenant.id, email="a@example.com", first_name="Alice", validation_status="VALID")
        contact2 = Contact(tenant_id=tenant.id, email="b@example.com", first_name="Bob", validation_status="VALID")
        template = Template(tenant_id=tenant.id, name="Intro")
        session.add_all([sender, contact, contact2, template])
        session.flush()
        version = TemplateVersion(tenant_id=tenant.id, template_id=template.id, version_number=1, subject_template="Hi {{first_name}}", html_body="<p>Hi {{first_name}}</p>", status="ACTIVE", variable_manifest=["first_name"])
        session.add(version)
        contact_list = ContactList(tenant_id=tenant.id, name="Prospects")
        session.add(contact_list)
        session.flush()
        session.add_all([
            ContactListMember(tenant_id=tenant.id, contact_list_id=contact_list.id, contact_id=contact.id),
            ContactListMember(tenant_id=tenant.id, contact_list_id=contact_list.id, contact_id=contact2.id),
        ])
        segment = ContactSegment(tenant_id=tenant.id, name="All", filters={"match": "all", "conditions": [{"field": "email", "operator": "is_not_empty", "value": None}]})
        session.add(segment)
        session.commit()
        yield session, tenant.id, sender.id, version.id, contact_list.id, segment.id, contact.id
    engine.dispose()


def make_campaign(session, tenant_id, sender_id, version_id, list_id, *, actor=None):
    service = CampaignService(session, tenant_id, actor or uuid4())
    return service.create(CampaignCreate(name="Outreach", objective="Start a conversation", sender_id=sender_id, template_version_id=version_id, recipient_list_id=list_id, timezone="UTC", variable_mapping={"first_name": "first_name"})), service


def test_validation_gate_pass(campaign_session) -> None:
    session, tenant_id, sender_id, version_id, list_id, _seg, _c = campaign_session
    campaign, service = make_campaign(session, tenant_id, sender_id, version_id, list_id)
    result = service.validate(campaign.id)
    assert result.level in {"PASS", "WARNING"}


def test_validation_gate_blocks_missing_template(campaign_session) -> None:
    session, tenant_id, sender_id, _version_id, list_id, _seg, _c = campaign_session
    service = CampaignService(session, tenant_id, uuid4())
    campaign = service.create(CampaignCreate(name="No template", objective="x", sender_id=sender_id, recipient_list_id=list_id))
    result = service.validate(campaign.id)
    assert result.level == "BLOCK"
    csv = {check.name for check in result.checks if check.outcome == "BLOCK"}
    assert "template_selected" in csv


def test_validation_gate_blocks_suppressed(campaign_session) -> None:
    session, tenant_id, sender_id, version_id, list_id, _seg, _contact_id = campaign_session
    session.add(Suppression(tenant_id=tenant_id, email="a@example.com", reason="MANUAL_BLOCK", source="test", effective_at=datetime.now(UTC)))
    session.commit()
    campaign, service = make_campaign(session, tenant_id, sender_id, version_id, list_id)
    result = service.validate(campaign.id)
    assert result.level == "BLOCK"
    assert any(check.name == "recipient_suppression" and check.outcome == "BLOCK" for check in result.checks)


def test_approve_requires_validation_pass_and_immutable_snapshot(campaign_session) -> None:
    session, tenant_id, sender_id, version_id, list_id, _seg, _c = campaign_session
    campaign, service = make_campaign(session, tenant_id, sender_id, version_id, list_id)
    service.transition(campaign.id, "REVIEW")
    approved = service.approve(campaign.id)
    assert approved.status == "APPROVED"
    assert approved.approved_at is not None
    immutable = [v for v in approved.versions if v.is_immutable]
    assert len(immutable) == 1
    assert len(immutable[0].snapshot["recipient_ids"]) == 2


def test_approve_blocked_without_recipients_or_template(campaign_session) -> None:
    session, tenant_id, sender_id, _version_id, _list_id, _seg, _c = campaign_session
    service = CampaignService(session, tenant_id, uuid4())
    campaign = service.create(CampaignCreate(name="Empty", objective="x", sender_id=sender_id))
    service.transition(campaign.id, "REVIEW")
    with pytest.raises(CampaignError):
        service.approve(campaign.id)


def test_recipient_rendered_data_snapshot_on_approval(campaign_session) -> None:
    session, tenant_id, sender_id, version_id, list_id, _seg, _c = campaign_session
    campaign, service = make_campaign(session, tenant_id, sender_id, version_id, list_id)
    service.transition(campaign.id, "REVIEW")
    approved = service.approve(campaign.id)
    rendered = {r.contact_id: r.rendered_data for r in approved.recipients}
    assert len(rendered) == 2
    assert any(data.get("first_name") == "Alice" for data in rendered.values())


def test_recipient_snapshot_not_affected_by_later_contact_edit(campaign_session) -> None:
    session, tenant_id, sender_id, version_id, list_id, _seg, contact_id = campaign_session
    campaign, service = make_campaign(session, tenant_id, sender_id, version_id, list_id)
    service.transition(campaign.id, "REVIEW")
    service.approve(campaign.id)
    contact = session.get(Contact, contact_id)
    contact.first_name = "Renamed"
    session.commit()
    refreshed = service._campaign(campaign.id)
    snapshot = {r.contact_id: r.rendered_data.get("first_name") for r in refreshed.recipients}
    assert snapshot[contact_id] == "Alice"


def test_material_change_on_approved_returns_to_review_and_new_version(campaign_session) -> None:
    session, tenant_id, sender_id, version_id, list_id, _seg, _c = campaign_session
    campaign, service = make_campaign(session, tenant_id, sender_id, version_id, list_id)
    service.transition(campaign.id, "REVIEW")
    approved = service.approve(campaign.id)
    assert approved.status == "APPROVED"
    before = len(approved.versions)
    edited = service.update(campaign.id, CampaignUpdate(name="Outreach v2", objective="New goal"))
    assert edited.status == "REVIEW"
    assert edited.approved_at is None
    assert len(edited.versions) == before + 1


def test_audit_events_campaign_created_and_approved(campaign_session) -> None:
    session, tenant_id, sender_id, version_id, list_id, _seg, _c = campaign_session
    campaign, service = make_campaign(session, tenant_id, sender_id, version_id, list_id)
    service.transition(campaign.id, "REVIEW")
    service.approve(campaign.id)
    actions = list(session.scalars(select(AuditLog.action).where(AuditLog.tenant_id == tenant_id)))
    assert "CAMPAIGN_CREATED" in actions
    assert "CAMPAIGN_APPROVED" in actions


def test_segment_based_recipients(campaign_session) -> None:
    session, tenant_id, sender_id, version_id, _list_id, seg_id, _c = campaign_session
    service = CampaignService(session, tenant_id, uuid4())
    campaign = service.create(CampaignCreate(name="Segmented", objective="x", sender_id=sender_id, template_version_id=version_id, segment_id=seg_id, variable_mapping={"first_name": "first_name"}))
    assert len(campaign.recipients) == 2
    service.transition(campaign.id, "REVIEW")
    assert service.approve(campaign.id).status == "APPROVED"


def test_non_material_update_does_not_reversion(campaign_session) -> None:
    session, tenant_id, sender_id, version_id, list_id, _seg, _c = campaign_session
    campaign, service = make_campaign(session, tenant_id, sender_id, version_id, list_id)
    service.transition(campaign.id, "REVIEW")
    service.approve(campaign.id)
    before = len(service._campaign(campaign.id).versions)
    service.update(campaign.id, CampaignUpdate(name="Outreach v2"))
    edited = service._campaign(campaign.id)
    assert edited.status == "REVIEW"
    assert len(edited.versions) == before + 1
