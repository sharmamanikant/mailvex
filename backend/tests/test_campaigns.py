from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    CampaignVersion,
    Contact,
    ContactList,
    ContactListMember,
    EmailAccount,
    Template,
    TemplateVersion,
    Tenant,
)
from app.schemas.campaigns import CampaignCreate
from app.services.campaigns import CampaignError, CampaignNotFoundError, CampaignService


@pytest.fixture()
def campaign_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'campaigns.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Campaign Tenant", slug=f"campaign-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        sender = EmailAccount(tenant_id=tenant.id, provider="SMTP", email="sender@example.com", status="CONNECTED")
        contact = Contact(tenant_id=tenant.id, email="recipient@example.com")
        template = Template(tenant_id=tenant.id, name="Intro")
        session.add_all([sender, contact, template])
        session.flush()
        version = TemplateVersion(tenant_id=tenant.id, template_id=template.id, version_number=1, subject_template="Hi", html_body="<p>Hi</p>", status="ACTIVE", variable_manifest=[])
        session.add(version)
        contact_list = ContactList(tenant_id=tenant.id, name="Prospects")
        session.add(contact_list)
        session.flush()
        session.add(ContactListMember(tenant_id=tenant.id, contact_list_id=contact_list.id, contact_id=contact.id))
        session.commit()
        yield session, tenant.id, sender.id, version.id, contact_list.id
    engine.dispose()


def make_campaign(session, tenant_id, sender_id, version_id, list_id):
    return CampaignService(session, tenant_id, uuid4()).create(CampaignCreate(name="Outreach", objective="Start a conversation", sender_id=sender_id, template_version_id=version_id, recipient_list_id=list_id, timezone_policy="UTC", follow_up_policy={"enabled": False}, variable_mapping={}))


def test_campaign_creation_and_tenant_scoped_references(campaign_session) -> None:
    session, tenant_id, sender_id, version_id, list_id = campaign_session
    campaign = make_campaign(session, tenant_id, sender_id, version_id, list_id)
    assert campaign.status == "DRAFT"
    assert len(campaign.recipients) == 1
    with pytest.raises(CampaignNotFoundError):
        CampaignService(session, uuid4())._campaign(campaign.id)


def test_approval_creates_immutable_snapshot(campaign_session) -> None:
    session, tenant_id, sender_id, version_id, list_id = campaign_session
    service = CampaignService(session, tenant_id, uuid4())
    campaign = make_campaign(session, tenant_id, sender_id, version_id, list_id)
    with pytest.raises(CampaignError):
        service.transition(campaign.id, "APPROVED")
    service.transition(campaign.id, "REVIEW")
    approved = service.transition(campaign.id, "APPROVED")
    assert approved.status == "APPROVED"
    snapshot = session.query(CampaignVersion).filter_by(campaign_id=campaign.id).one()
    assert snapshot.is_immutable is True
    assert snapshot.snapshot["objective"] == "Start a conversation"


def test_invalid_transitions_and_duplicate(campaign_session) -> None:
    session, tenant_id, sender_id, version_id, list_id = campaign_session
    service = CampaignService(session, tenant_id, uuid4())
    campaign = make_campaign(session, tenant_id, sender_id, version_id, list_id)
    with pytest.raises(CampaignError):
        service.transition(campaign.id, "RUNNING")
    duplicate = service.duplicate(campaign.id, "Outreach Copy")
    assert duplicate.status == "DRAFT"
    assert duplicate.name == "Outreach Copy"
