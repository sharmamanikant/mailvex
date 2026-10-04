from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.models import (
    Base,
    Campaign,
    CampaignRecipient,
    Contact,
    DeliveryJob,
    EmailAccount,
    Message,
    NormalizedDeliveryEvent,
    Tenant,
)
from app.services.reporting import ReportingService


def _engine(tmp_path, name: str):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    return engine


def _add_tenant(session, slug_suffix: str) -> Tenant:
    tenant = Tenant(name="T", slug=f"t-{slug_suffix}")
    session.add(tenant)
    session.flush()
    return tenant


def _sender(session, tenant, email: str, status="CONNECTED", health=None):
    s = EmailAccount(
        tenant_id=tenant.id,
        provider="GOOGLE",
        email=email,
        status=status,
        health_score=health,
    )
    session.add(s)
    session.flush()
    return s


def _contact(session, tenant, email: str, status="ACTIVE", *, validation="UNKNOWN", suppression="CLEAR"):
    c = Contact(
        tenant_id=tenant.id,
        email=email,
        status=status,
        validation_status=validation,
        suppression_status=suppression,
    )
    session.add(c)
    session.flush()
    return c


def _campaign(session, tenant, sender, name: str, status: str):
    c = Campaign(
        tenant_id=tenant.id,
        name=name,
        objective="obj",
        sender_id=sender.id,
        status=status,
    )
    session.add(c)
    session.flush()
    return c


def _recipient(session, tenant, campaign, contact):
    r = CampaignRecipient(
        tenant_id=tenant.id, campaign_id=campaign.id, contact_id=contact.id
    )
    session.add(r)
    session.flush()
    return r


def _job(session, tenant, campaign, sender, contact, status, *, failure_code=None, completed=True):
    j = DeliveryJob(
        tenant_id=tenant.id,
        campaign_id=campaign.id,
        recipient_id=contact.id,
        sender_id=sender.id,
        scheduled_at=datetime.now(UTC),
        status=status,
        failure_code=failure_code,
        completed_at=datetime.now(UTC) if completed else None,
    )
    session.add(j)
    session.flush()
    return j


def _message(session, tenant, campaign, sender, contact, status="DELIVERED"):
    m = Message(
        tenant_id=tenant.id,
        campaign_id=campaign.id,
        sender_id=sender.id,
        contact_id=contact.id,
        subject="s",
        status=status,
    )
    session.add(m)
    session.flush()
    return m


def _event(session, tenant, message, event_type, when=None):
    e = NormalizedDeliveryEvent(
        tenant_id=tenant.id,
        provider="GOOGLE",
        provider_event_id=f"ev-{uuid4().hex[:12]}",
        message_id=message.id,
        recipient="r@example.com",
        event_type=event_type,
        event_time=when or datetime.now(UTC),
    )
    session.add(e)
    session.flush()
    return e


@pytest.fixture()
def db(tmp_path):
    engine = _engine(tmp_path, "reporting.db")
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    tenant = _add_tenant(session, "a")
    sender = _sender(session, tenant, "sender-a@example.com", "CONNECTED", 90)

    # --- Contacts -------------------------------------------------------
    c1 = _contact(session, tenant, "c1@example.com", "ACTIVE")
    c2 = _contact(session, tenant, "c2@example.com", "UNSUBSCRIBED")
    c3 = _contact(session, tenant, "c3@example.com", "INVALID")
    c4 = _contact(session, tenant, "c4@example.com", "ACTIVE", suppression="SUPPRESSED")
    _contact(session, tenant, "c5@example.com", "BOUNCED")
    _contact(session, tenant, "c6@example.com", "INACTIVE")
    _contact(session, tenant, "c7@example.com", "ACTIVE", validation="INVALID")

    # --- Campaigns ------------------------------------------------------
    cam1 = _campaign(session, tenant, sender, "Runner", "RUNNING")
    _campaign(session, tenant, sender, "Scheduled", "SCHEDULED")
    _campaign(session, tenant, sender, "Done", "COMPLETED")
    _campaign(session, tenant, sender, "Approved", "APPROVED")
    _campaign(session, tenant, sender, "Drafty", "DRAFT")

    # cam1 has 4 recipients (c1-c4)
    _recipient(session, tenant, cam1, c1)
    _recipient(session, tenant, cam1, c2)
    _recipient(session, tenant, cam1, c3)
    _recipient(session, tenant, cam1, c4)

    # --- DeliveryJobs for cam1 (via sender); one per recipient ------------
    _job(session, tenant, cam1, sender, c1, "SENT")
    _job(session, tenant, cam1, sender, c2, "DELIVERED")
    _job(
        session, tenant, cam1, sender, c3, "FAILED",
        failure_code="PROVIDER_THROTTLED",
    )
    _job(session, tenant, cam1, sender, c4, "BLOCKED")

    # --- More senders ----------------------------------------------------
    _sender(session, tenant, "sender2@example.com", "HEALTHY", 70)
    _sender(session, tenant, "sender3@example.com", "SUSPENDED", 55)
    _sender(session, tenant, "sender4@example.com", "CONNECTED", 30)

    # --- Messages + normalized events (for cam1, via sender) -------------
    m1 = _message(session, tenant, cam1, sender, c1)
    _event(session, tenant, m1, "DELIVERED")
    m2 = _message(session, tenant, cam1, sender, c2)
    _event(session, tenant, m2, "HARD_BOUNCE")
    m3 = _message(session, tenant, cam1, sender, c1)
    _event(session, tenant, m3, "COMPLAINT")
    m4 = _message(session, tenant, cam1, sender, c2)
    _event(session, tenant, m4, "UNSUBSCRIBED")
    m5 = _message(session, tenant, cam1, sender, c1)
    _event(session, tenant, m5, "TEMPORARY_FAILURE")

    session.commit()
    return session, tenant, sender


def test_contact_report_counts(db) -> None:
    session, tenant, _ = db
    report = ReportingService(session, tenant.id).contact_report()
    assert report["total"] == 7
    assert report["active"] == 3  # c1, c4, c7
    assert report["unsubscribed"] == 1
    assert report["suppressed"] == 1  # c4
    assert report["invalid"] == 2  # c3 status + c7 validation
    assert report["inactive"] == 1
    assert report["bounced"] == 1


def test_campaign_overview_counts(db) -> None:
    session, tenant, _ = db
    report = ReportingService(session, tenant.id).campaign_overview()
    assert report["total"] == 5
    assert report["active"] == 1  # RUNNING
    assert report["scheduled"] == 2  # SCHEDULED + APPROVED
    assert report["completed"] == 1


def test_sender_overview_counts(db) -> None:
    session, tenant, _ = db
    report = ReportingService(session, tenant.id).sender_overview()
    assert report["total"] == 4
    assert report["connected"] == 3  # all except SUSPENDED
    assert report["healthy"] == 2  # s1 (score 90) + s2 (status HEALTHY)
    assert report["needs_attention"] == 1  # SUSPENDED


def test_delivery_overview_counts(db) -> None:
    session, tenant, _ = db
    report = ReportingService(session, tenant.id).delivery_overview()
    assert report["sent"] == 2  # SENT + DELIVERED jobs
    assert report["delivered"] == 1
    assert report["bounced"] == 1
    assert report["unsubscribed"] == 1
    assert report["complaints"] == 1
    assert report["temporary_failures"] == 1
    assert report["blocked"] == 1
    assert report["failed"] == 1


def test_sender_report_rows(db) -> None:
    session, tenant, _ = db
    rows = ReportingService(session, tenant.id).sender_report()
    by_email = {r["sender_email"]: r for r in rows}
    assert len(rows) == 4

    s1 = by_email["sender-a@example.com"]
    assert s1["messages"] == 4
    assert s1["successful"] == 2
    assert s1["failed"] == 1
    assert s1["blocked"] == 1
    assert s1["provider_throttling"] == 1
    assert s1["temporary_failures"] == 1
    assert s1["health_score"] == 90
    assert s1["health"] == "HEALTHY"

    assert by_email["sender3@example.com"]["health"] == "NEEDS_ATTENTION"
    assert by_email["sender4@example.com"]["health"] == "NEEDS_ATTENTION"
    assert by_email["sender2@example.com"]["health"] == "HEALTHY"


def test_campaign_report_rows(db) -> None:
    session, tenant, _ = db
    rows = ReportingService(session, tenant.id).campaign_report()
    by_name = {r["campaign_name"]: r for r in rows}
    runner = by_name["Runner"]
    assert runner["recipients"] == 4
    assert runner["sent"] == 2
    assert runner["delivered"] == 1
    assert runner["bounced"] == 1
    assert runner["unsubscribed"] == 1
    assert runner["complaints"] == 1
    assert runner["temporary_failures"] == 1
    assert runner["blocked"] == 1
    assert runner["failed"] == 1
    assert runner["delivery_rate"] == 25.0

    assert by_name["Drafty"]["sent"] == 0
    assert by_name["Drafty"]["recipients"] == 0


def test_dashboard_shape(db) -> None:
    session, tenant, _ = db
    dash = ReportingService(session, tenant.id).dashboard()
    assert dash["contacts"]["total"] == 7
    assert dash["campaigns"]["total"] == 5
    assert dash["delivery"]["sent"] == 2
    assert dash["senders"]["total"] == 4
    assert dash["tenant_id"] == str(tenant.id)


def test_tenant_isolation(db) -> None:
    session, tenant, _ = db
    other = _add_tenant(session, "b")
    _contact(session, other, "other@example.com", "ACTIVE")
    session.commit()

    svc = ReportingService(session, tenant.id)
    assert svc.contact_report()["total"] == 7
    assert svc.campaign_overview()["total"] == 5
    assert svc.sender_overview()["total"] == 4

    other_svc = ReportingService(session, other.id)
    assert other_svc.contact_report()["total"] == 1
    assert other_svc.campaign_overview()["total"] == 0
    assert other_svc.sender_overview()["total"] == 0


def test_legacy_message_fallback(db) -> None:
    session, _, _ = db
    # A tenant with Messages but no DeliveryJobs uses legacy counts.
    legacy_tenant = _add_tenant(session, "legacy")
    legacy_sender = _sender(session, legacy_tenant, "legacy@sender.com", "CONNECTED", 90)
    legacy_cam = _campaign(session, legacy_tenant, legacy_sender, "Legacy", "COMPLETED")
    lc = _contact(session, legacy_tenant, "legacy-c@example.com")
    m1 = _message(session, legacy_tenant, legacy_cam, legacy_sender, lc, "SENT")
    _message(session, legacy_tenant, legacy_cam, legacy_sender, lc, "BOUNCED")
    _event(session, legacy_tenant, m1, "DELIVERED")
    session.commit()

    report = ReportingService(session, legacy_tenant.id).delivery_overview()
    assert report["sent"] == 1  # from legacy Message counts
    assert report["delivered"] == 1  # events still counted
    assert report["bounced"] == 1  # Message status BOUNCED
    assert report["blocked"] == 0
    assert report["failed"] == 0


def test_date_filter_custom(db) -> None:
    session, tenant, _ = db
    old = _contact(session, tenant, "old@example.com", "ACTIVE")
    old.created_at = datetime.now(UTC) - timedelta(days=200)
    session.commit()

    svc = ReportingService(session, tenant.id)
    # Without a window, the old contact is counted.
    assert svc.contact_report()["total"] == 8
    # A 7d window excludes the old contact (created 200 days ago).
    assert svc.contact_report(range="7d")["total"] == 7
    # A custom window in the recent past includes only contacts created then.
    report = svc.contact_report(
        range="custom",
        start_date=(datetime.now(UTC) - timedelta(days=250)).date().isoformat(),
        end_date=(datetime.now(UTC) - timedelta(days=190)).date().isoformat(),
    )
    assert report["total"] == 1  # only the old contact
    # A custom window before the old contact was created matches nothing.
    report = svc.contact_report(
        range="custom",
        start_date=(datetime.now(UTC) - timedelta(days=400)).date().isoformat(),
        end_date=(datetime.now(UTC) - timedelta(days=300)).date().isoformat(),
    )
    assert report["total"] == 0


def test_csv_export_contacts(db) -> None:
    session, tenant, _ = db
    csv_data = ReportingService(session, tenant.id).export_csv("contacts")
    lines = csv_data.splitlines()
    assert lines[0] == "total,active,unsubscribed,suppressed,invalid,inactive,bounced"
    # only one data row
    assert len(lines) == 2
    assert lines[1].startswith("7,3,1,1,2,1,1")


def test_csv_export_campaigns(db) -> None:
    session, tenant, _ = db
    csv_data = ReportingService(session, tenant.id).export_csv("campaigns")
    header = csv_data.splitlines()[0]
    assert header == (
        "campaign_id,campaign_name,status,recipients,sent,delivered,bounced,"
        "blocked,unsubscribed,complaints,failed,delivery_rate"
    )
    # every campaign has a CSV row
    data_lines = csv_data.strip().splitlines()[1:]
    assert len(data_lines) == 5
