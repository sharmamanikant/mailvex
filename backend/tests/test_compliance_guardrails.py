from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Base,
    CampaignSender,
    Contact,
    ContactList,
    ContactListMember,
    EmailAccount,
    Message,
    MessageEvent,
    SenderAccount,
    SenderConnection,
    Template,
    TemplateVersion,
    Tenant,
    User,
)
from app.schemas.campaigns import CampaignCreate
from app.services.campaigns import CampaignService
from app.services.compliance_profile import ComplianceProfileService
from app.services.compliance_status import (
    BLOCKED,
    COMPLIANT,
    REVIEW_REQUIRED,
    ComplianceStatusService,
    normalize_reason,
)
from app.services.policies import PolicyError, PolicyService
from app.services.safety import HIGH_BOUNCE_RATE, SenderSafetyError, SenderSafetyService


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'guardrails.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Guardrail Tenant", slug=f"guardrail-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        session.add(User(tenant_id=tenant.id, email="owner@example.com", display_name="Owner"))
        session.commit()
        yield session, tenant.id
    engine.dispose()


def make_email_account(session: Session, tenant_id, status: str = "CONNECTED") -> EmailAccount:
    account = EmailAccount(tenant_id=tenant_id, provider="SMTP", email=f"sender-{uuid4().hex[:6]}@example.com", status=status)
    session.add(account)
    session.flush()
    return account


def make_sender_account(session: Session, tenant_id, email: str | None = None) -> SenderAccount:
    connection = SenderConnection(tenant_id=tenant_id, provider="GOOGLE", connection_type="OAUTH", status="CONNECTED")
    session.add(connection)
    session.flush()
    sender = SenderAccount(
        tenant_id=tenant_id,
        connection_id=connection.id,
        email=email or f"pool-{uuid4().hex[:6]}@example.com",
        provider="GOOGLE",
        status="ACTIVE",
    )
    session.add(sender)
    session.flush()
    return sender


def make_campaign_fixture(session: Session, tenant_id):
    """Seed a connected legacy sender, two valid contacts, and an active template."""
    sender = make_email_account(session, tenant_id)
    contact = Contact(tenant_id=tenant_id, email="a@example.com", first_name="Alice", validation_status="VALID")
    contact2 = Contact(tenant_id=tenant_id, email="b@example.com", first_name="Bob", validation_status="VALID")
    template = Template(tenant_id=tenant_id, name="Intro")
    session.add_all([contact, contact2, template])
    session.flush()
    version = TemplateVersion(
        tenant_id=tenant_id,
        template_id=template.id,
        version_number=1,
        subject_template="Hi {{first_name}}",
        html_body="<p>Hi {{first_name}}</p>",
        status="ACTIVE",
        variable_manifest=["first_name"],
    )
    contact_list = ContactList(tenant_id=tenant_id, name="Prospects")
    session.add_all([version, contact_list])
    session.flush()
    session.add_all(
        [
            ContactListMember(tenant_id=tenant_id, contact_list_id=contact_list.id, contact_id=contact.id),
            ContactListMember(tenant_id=tenant_id, contact_list_id=contact_list.id, contact_id=contact2.id),
        ]
    )
    session.commit()
    return sender.id, version.id, contact_list.id


def make_campaign(session: Session, tenant_id, sender_id, version_id, list_id, actor):
    service = CampaignService(session, tenant_id, actor)
    campaign = service.create(
        CampaignCreate(
            name="Outreach",
            objective="Start a conversation",
            sender_id=sender_id,
            template_version_id=version_id,
            recipient_list_id=list_id,
            timezone="UTC",
            variable_mapping={"first_name": "first_name"},
        )
    )
    return campaign, service


def add_send_telemetry(session: Session, tenant_id, sender: SenderAccount, count: int, bounces: int) -> None:
    legacy = make_email_account(session, tenant_id)
    contact = Contact(tenant_id=tenant_id, email=f"r-{uuid4().hex[:6]}@example.com", validation_status="VALID")
    session.add(contact)
    session.flush()
    message: Message | None = None
    for index in range(count):
        message = Message(
            tenant_id=tenant_id,
            sender_id=legacy.id,
            sender_account_id=sender.id,
            contact_id=contact.id,
            recipient=f"r{index}@example.com",
            subject="Hi",
            msg_type="CAMPAIGN",
        )
        session.add(message)
    session.flush()
    for _ in range(bounces):
        session.add(
            MessageEvent(
                tenant_id=tenant_id,
                message_id=message.id,
                event_type="HARD_BOUNCE",
                occurred_at=datetime.now(UTC),
                provider_event_id=f"evt-{uuid4()!s}",
            )
        )
    session.commit()


# --------------------------------------------------------------------- #
# Policies (PolicyService)
# --------------------------------------------------------------------- #
def test_policy_seeded_and_acceptance_flow(db) -> None:
    session, tenant_id = db
    user_id = session.scalars(select(User.id).where(User.tenant_id == tenant_id)).one()
    service = PolicyService(session, tenant_id, user_id)

    status = {item["policy_type"]: item for item in service.status(user_id)}
    assert set(status) == {"terms_of_service", "acceptable_use"}
    assert all(not item["accepted"] for item in status.values())
    assert not service.is_tenant_accepted("terms_of_service")

    terms_version = service.current_version("terms_of_service")
    accepted = service.accept("terms_of_service", terms_version, ip_address="127.0.0.1")
    assert accepted.user_id == user_id
    assert service.is_accepted("terms_of_service", user_id)
    assert not service.is_accepted("acceptable_use", user_id)
    assert service.is_tenant_accepted("terms_of_service")
    assert not service.is_tenant_accepted("acceptable_use")

    rows = session.scalars(
        select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == "POLICY_ACCEPTED")
    ).all()
    assert len(rows) == 1


def test_policy_accept_requires_authenticated_user(db) -> None:
    session, tenant_id = db
    service = PolicyService(session, tenant_id)
    with pytest.raises(PolicyError):
        service.accept("terms_of_service", service.current_version("terms_of_service"))


def test_policy_unknown_type_rejected(db) -> None:
    session, tenant_id = db
    service = PolicyService(session, tenant_id, uuid4())
    with pytest.raises(PolicyError):
        service.get_document("privacy")


# --------------------------------------------------------------------- #
# Compliance profile (ComplianceProfileService)
# --------------------------------------------------------------------- #
def test_profile_defaults_seeded(db) -> None:
    session, tenant_id = db
    profile = ComplianceProfileService(session, tenant_id).get()
    assert profile.compliance_profile == "STANDARD"
    assert profile.jurisdiction == "UNSPECIFIED"
    assert profile.require_policy_acceptance is False
    assert profile.require_consent_metadata is False
    assert profile.require_list_unsubscribe_header is True
    assert profile.safety_thresholds["hard_bounce_rate"] == 0.05
    assert profile.retention_policy["message_events_days"] == 365


def test_profile_update_merges_thresholds_and_audits(db) -> None:
    session, tenant_id = db
    actor = uuid4()
    profile = ComplianceProfileService(session, tenant_id, actor).update(
        jurisdiction="EU",
        require_policy_acceptance=True,
        safety_thresholds={"hard_bounce_rate": 0.08},
    )
    assert profile.jurisdiction == "EU"
    assert profile.require_policy_acceptance is True
    assert profile.safety_thresholds["hard_bounce_rate"] == 0.08
    assert profile.safety_thresholds["complaint_rate"] == 0.001
    assert profile.safety_thresholds["window_days"] == 7
    actions = list(session.scalars(select(AuditLog.action).where(AuditLog.tenant_id == tenant_id)))
    assert "COMPLIANCE_PROFILE_UPDATED" in actions

    safety_audit = session.scalar(
        select(AuditLog).where(AuditLog.tenant_id == tenant_id, AuditLog.action == "COMPLIANCE_PROFILE_UPDATED")
    )
    assert safety_audit is not None
    assert "jurisdiction" in safety_audit.audit_metadata["changes"]


def test_unsafe_threshold_sanity_ranges(db) -> None:
    svc = ComplianceProfileService
    assert svc.unsafe_threshold("hard_bounce_rate", 1.0)
    assert svc.unsafe_threshold("hard_bounce_rate", -0.1)
    assert svc.unsafe_threshold("hard_bounce_rate", True)
    assert not svc.unsafe_threshold("hard_bounce_rate", 0.05)
    assert svc.unsafe_threshold("complaint_rate", 0.0)
    assert svc.unsafe_threshold("window_days", 0)
    assert not svc.unsafe_threshold("window_days", 7)
    assert not svc.unsafe_threshold("min_sample_size", 30)
    assert svc.unsafe_threshold("min_sample_size", -5)


# --------------------------------------------------------------------- #
# Sender safety (SenderSafetyService)
# --------------------------------------------------------------------- #
def test_safety_compliant_without_telemetry(db) -> None:
    session, tenant_id = db
    sender = make_sender_account(session, tenant_id)
    state, reasons = SenderSafetyService(session, tenant_id).evaluate(sender)
    assert state == "COMPLIANT"
    assert reasons == []


def test_safety_review_required_on_high_bounce(db) -> None:
    session, tenant_id = db
    sender = make_sender_account(session, tenant_id)
    add_send_telemetry(session, tenant_id, sender, count=40, bounces=3)
    state, reasons = SenderSafetyService(session, tenant_id).evaluate(sender)
    assert state == "REVIEW_REQUIRED"
    assert HIGH_BOUNCE_RATE in reasons


def test_safety_apply_pauses_and_release_reenables(db) -> None:
    session, tenant_id = db
    actor = uuid4()
    sender = make_sender_account(session, tenant_id)
    service = SenderSafetyService(session, tenant_id, actor)

    service.apply(sender, REVIEW_REQUIRED, [HIGH_BOUNCE_RATE])
    assert sender.compliance_status == "REVIEW_REQUIRED"
    assert sender.paused_at is not None
    assert sender.resume_guard is True
    actions = list(session.scalars(select(AuditLog.action).where(AuditLog.tenant_id == tenant_id)))
    assert "SENDER_PAUSED" in actions

    released = service.release(sender.id, review_note="Reviewed bounce causes; sender OK")
    assert released.compliance_status == "COMPLIANT"
    assert released.compliance_reasons == []
    assert released.paused_at is None
    assert released.resume_guard is False
    actions = list(session.scalars(select(AuditLog.action).where(AuditLog.tenant_id == tenant_id)))
    assert "SENDER_RE_ENABLED" in actions


def test_safety_paused_sender_never_auto_resumes(db) -> None:
    session, tenant_id = db
    sender = make_sender_account(session, tenant_id)
    service = SenderSafetyService(session, tenant_id, uuid4())
    service.apply(sender, REVIEW_REQUIRED, [HIGH_BOUNCE_RATE])
    service.apply(sender, "COMPLIANT", [])
    assert sender.compliance_status == "REVIEW_REQUIRED"
    assert sender.paused_at is not None
    assert sender.resume_guard is True


def test_safety_release_rejects_sender_not_paused(db) -> None:
    session, tenant_id = db
    sender = make_sender_account(session, tenant_id)
    service = SenderSafetyService(session, tenant_id, uuid4())
    with pytest.raises(SenderSafetyError):
        service.release(sender.id)


# --------------------------------------------------------------------- #
# Compliance status (ComplianceStatusService)
# --------------------------------------------------------------------- #
def test_normalize_reason_vocabulary(db) -> None:
    assert normalize_reason(["CAMPAIGN_BLOCKED_SUPPRESSION"]) == ["SUPPRESSION_CHECK_FAILED"]
    assert normalize_reason(["CAMPAIGN_BLOCKED_CONSENT", "CAMPAIGN_BLOCKED_CONSENT"]) == ["CONSENT_METADATA_REQUIRED"]
    assert normalize_reason(["UNSUBSCRIBE_NOT_CONFIGURED"]) == ["UNSUBSCRIBE_NOT_CONFIGURED"]
    assert normalize_reason(["RATE_LIMIT"]) == ["RATE_LIMIT"]
    assert normalize_reason(None) == []


def test_legacy_sender_status(db) -> None:
    session, tenant_id = db
    svc = ComplianceStatusService(session, tenant_id)

    healthy = make_email_account(session, tenant_id)
    state, reasons = svc.sender_status(healthy)
    assert state == COMPLIANT
    assert reasons == []

    critical = make_email_account(session, tenant_id, status="HEALTH_CRITICAL")
    state, reasons = svc.sender_status(critical)
    assert state == BLOCKED
    assert reasons == ["SENDER_HEALTH_CRITICAL"]

    disconnected = make_email_account(session, tenant_id, status="REAUTH_REQUIRED")
    state, reasons = svc.sender_status(disconnected)
    assert state == BLOCKED
    assert reasons == ["SENDER_AUTHENTICATION_REQUIRED"]


def test_system_b_sender_status(db) -> None:
    session, tenant_id = db
    svc = ComplianceStatusService(session, tenant_id)

    sender = make_sender_account(session, tenant_id)
    state, _ = svc.sender_status(sender)
    assert state == COMPLIANT

    sender.status = "DISABLED"
    state, reasons = svc.sender_status(sender)
    assert state == BLOCKED
    assert "SENDER_NOT_AUTHENTICATED" in reasons


def test_campaign_status_escalates_when_pool_sender_paused(db) -> None:
    session, tenant_id = db
    actor = uuid4()
    sender_id, version_id, list_id = make_campaign_fixture(session, tenant_id)
    campaign, _ = make_campaign(session, tenant_id, sender_id, version_id, list_id, actor)

    pool_sender = make_sender_account(session, tenant_id)
    SenderSafetyService(session, tenant_id, actor).apply(pool_sender, REVIEW_REQUIRED, [HIGH_BOUNCE_RATE])
    session.add(CampaignSender(tenant_id=tenant_id, campaign_id=campaign.id, sender_id=pool_sender.id, enabled=True))
    session.commit()

    state, reasons = ComplianceStatusService(session, tenant_id, actor).campaign_status(campaign)
    assert state == REVIEW_REQUIRED
    assert HIGH_BOUNCE_RATE in reasons
    assert campaign.compliance_status == "REVIEW_REQUIRED"
    assert campaign.compliance_evaluated_at is not None


# --------------------------------------------------------------------- #
# Campaign pre-flight (CampaignService.validate)
# --------------------------------------------------------------------- #
def test_validate_blocks_consent_when_profile_requires(db) -> None:
    session, tenant_id = db
    actor = uuid4()
    sender_id, version_id, list_id = make_campaign_fixture(session, tenant_id)
    ComplianceProfileService(session, tenant_id, actor).update(require_consent_metadata=True)
    campaign, service = make_campaign(session, tenant_id, sender_id, version_id, list_id, actor)

    result = service.validate(campaign.id)
    blocked = {check.name for check in result.checks if check.outcome == "BLOCK"}
    assert "recipient_consent" in blocked
    consent = next(check for check in result.checks if check.name == "recipient_consent")
    assert consent.code == "CAMPAIGN_BLOCKED_CONSENT"


def test_validate_blocks_policy_acceptance_when_required(db) -> None:
    session, tenant_id = db
    actor = uuid4()
    sender_id, version_id, list_id = make_campaign_fixture(session, tenant_id)
    profile_service = ComplianceProfileService(session, tenant_id, actor)
    profile_service.update(require_policy_acceptance=True)
    campaign, service = make_campaign(session, tenant_id, sender_id, version_id, list_id, actor)

    result = service.validate(campaign.id)
    policy_check = next(
        (check for check in result.checks if check.name == "tenant_policy_accepted" and check.outcome == "BLOCK"),
        None,
    )
    assert policy_check is not None
    assert policy_check.code == "CAMPAIGN_BLOCKED_POLICY_ACCEPTANCE"

    service = PolicyService(session, tenant_id, actor)
    service.accept("terms_of_service", service.current_version("terms_of_service"))
    service.accept("acceptable_use", service.current_version("acceptable_use"))
    result = CampaignService(session, tenant_id, actor).validate(campaign.id)
    policy_check = next(check for check in result.checks if check.name == "tenant_policy_accepted")
    assert policy_check.outcome != "BLOCK"


def test_validate_persists_normalized_status_on_campaign(db) -> None:
    session, tenant_id = db
    actor = uuid4()
    sender_id, version_id, list_id = make_campaign_fixture(session, tenant_id)
    campaign, service = make_campaign(session, tenant_id, sender_id, version_id, list_id, actor)

    result = service.validate(campaign.id)
    session.flush()
    assert campaign.compliance_status == result.level
    assert campaign.compliance_evaluated_at is not None