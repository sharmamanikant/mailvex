from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, ClassVar, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.billing import EVENT_CAMPAIGN_CREATED, UsageService
from app.models import (
    Campaign,
    CampaignRecipient,
    CampaignSender,
    CampaignVersion,
    Contact,
    ContactCustomField,
    ContactListMember,
    ContactSegment,
    Domain,
    EmailAccount,
    SenderAccount,
    Suppression,
    SuppressionEntry,
    TemplateVersion,
    Unsubscribe,
)
from app.schemas.campaigns import CampaignCreate, CampaignUpdate
from app.schemas.segments import ContactSegmentFilter
from app.services.audit import AuditService
from app.services.compliance_profile import ComplianceProfileService


class CampaignError(ValueError):
    pass


class CampaignNotFoundError(LookupError):
    pass


@dataclass(frozen=True)
class ValidationCheck:
    name: str
    outcome: Literal["PASS", "WARNING", "BLOCK"]
    message: str
    remediation: str | None = None
    code: str | None = None


@dataclass(frozen=True)
class ValidationResult:
    campaign_id: UUID
    level: Literal["PASS", "WARNING", "BLOCK"]
    checks: tuple[ValidationCheck, ...] = field(default_factory=tuple)

    @property
    def blocks(self) -> tuple[ValidationCheck, ...]:
        return tuple(check for check in self.checks if check.outcome == "BLOCK")

    @property
    def warnings(self) -> tuple[ValidationCheck, ...]:
        return tuple(check for check in self.checks if check.outcome == "WARNING")


MATERIAL_FIELDS = {
    "name",
    "objective",
    "description",
    "sender_id",
    "template_version_id",
    "recipient_list_id",
    "segment_id",
    "recipient_ids",
    "timezone",
    "scheduled_at",
    "follow_up_policy",
    "variable_mapping",
    "schedule_config",
}


class CampaignService:
    TRANSITIONS: ClassVar[dict[str, set[str]]] = {
        "DRAFT": {"REVIEW", "CANCELLED"},
        "REVIEW": {"DRAFT", "APPROVED", "CANCELLED"},
        "APPROVED": {"SCHEDULED", "CANCELLED"},
        "SCHEDULED": {"RUNNING", "PAUSED", "CANCELLED"},
        "RUNNING": {"PAUSED", "COMPLETED", "CANCELLED"},
        "PAUSED": {"SCHEDULED", "CANCELLED"},
        "COMPLETED": set(),
        "CANCELLED": set(),
    }

    _PERSONALIZATION_FIELDS = (
        "first_name",
        "last_name",
        "email",
        "phone",
        "company",
        "designation",
        "location",
        "website",
        "industry",
        "source",
        "source_reference",
    )

    def __init__(self, session: Session, tenant_id: UUID, actor_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id

    def _audit(self, action: str, resource_id: UUID | None, metadata: dict[str, Any] | None = None) -> None:
        AuditService(self.session, self.tenant_id, self.actor_id).record(
            action, "campaign", resource_id, metadata
        )

    def _campaign(self, campaign_id: UUID) -> Campaign:
        campaign = self.session.scalar(
            select(Campaign)
            .options(selectinload(Campaign.recipients), selectinload(Campaign.versions))
            .where(Campaign.id == campaign_id, Campaign.tenant_id == self.tenant_id)
        )
        if campaign is None:
            raise CampaignNotFoundError("Campaign not found")
        return campaign

    def _load_recipients(self, payload: CampaignCreate | CampaignUpdate) -> list[Contact]:
        source_ids: list[UUID] = list(payload.recipient_ids or [])
        if payload.recipient_list_id is not None:
            source_ids = list(self.session.scalars(select(ContactListMember.contact_id).where(ContactListMember.contact_list_id == payload.recipient_list_id, ContactListMember.tenant_id == self.tenant_id)).all())
        elif payload.segment_id is not None:
            segment = self.session.scalar(select(ContactSegment).where(ContactSegment.id == payload.segment_id, ContactSegment.tenant_id == self.tenant_id))
            if segment is None:
                raise CampaignError("Segment is not available in this tenant")
            source_ids = self._segment_contact_ids(segment)
        if not source_ids:
            return []
        found = {contact.id: contact for contact in self.session.scalars(select(Contact).where(Contact.id.in_(source_ids), Contact.tenant_id == self.tenant_id)).all()}
        if len(found) != len(set(source_ids)):
            raise CampaignError("One or more recipients are outside this tenant")
        return [found[identifier] for identifier in source_ids]

    def _segment_contact_ids(self, segment: ContactSegment) -> list[UUID]:
        filters = ContactSegmentFilter.model_validate(segment.filters)
        from app.services.segments import SegmentService

        service = SegmentService(self.session, self.tenant_id)
        clause = service._build_clause(filters)
        from sqlalchemy import and_

        return list(self.session.scalars(select(Contact.id).where(and_(Contact.tenant_id == self.tenant_id, clause))).all())

    def _validate_references(self, payload: CampaignCreate | CampaignUpdate) -> None:
        sender_id = payload.sender_id
        if sender_id is not None:
            sender = self.session.scalar(select(EmailAccount).where(EmailAccount.id == sender_id, EmailAccount.tenant_id == self.tenant_id))
            if sender is None:
                raise CampaignError("Sender is not available in this tenant")
            if sender.status in {"HEALTH_CRITICAL", "DISABLED", "DISCONNECTED", "SUSPENDED", "REAUTH_REQUIRED"}:
                raise CampaignError("This sender is critical, disconnected, or needs re-authentication and cannot be used for new campaigns")
        template_id = payload.template_version_id
        if template_id is not None and self.session.scalar(select(TemplateVersion.id).where(TemplateVersion.id == template_id, TemplateVersion.tenant_id == self.tenant_id)) is None:
            raise CampaignError("Template is not available in this tenant")
        if payload.recipient_list_id is not None and self.session.scalar(select(ContactListMember.id).where(ContactListMember.contact_list_id == payload.recipient_list_id, ContactListMember.tenant_id == self.tenant_id)) is None:
            raise CampaignError("Recipient list is empty or unavailable")
        if payload.segment_id is not None and self.session.scalar(select(ContactSegment.id).where(ContactSegment.id == payload.segment_id, ContactSegment.tenant_id == self.tenant_id)) is None:
            raise CampaignError("Segment is not available in this tenant")

    def _recipient_ids(self, payload: CampaignCreate | CampaignUpdate) -> list[UUID]:
        return [contact.id for contact in self._load_recipients(payload)]

    @staticmethod
    def response_data(campaign: Campaign) -> dict[str, Any]:
        return {"id": campaign.id, "tenant_id": campaign.tenant_id, "name": campaign.name, "objective": campaign.objective, "description": campaign.description, "sender_id": campaign.sender_id, "template_version_id": campaign.template_version_id, "recipient_list_id": campaign.recipient_list_id, "segment_id": campaign.segment_id, "timezone": campaign.timezone, "scheduled_at": campaign.scheduled_at, "status": campaign.status, "schedule_config": campaign.schedule_config, "follow_up_policy": campaign.schedule_config.get("follow_up_policy", {}), "variable_mapping": campaign.schedule_config.get("variable_mapping", {}), "recipient_count": len(campaign.recipients), "approved_by_id": campaign.approved_by_id, "approved_at": campaign.approved_at, "compliance_status": campaign.compliance_status, "compliance_reasons": campaign.compliance_reasons or [], "compliance_evaluated_at": campaign.compliance_evaluated_at, "created_at": campaign.created_at, "updated_at": campaign.updated_at}

    def create(self, payload: CampaignCreate) -> Campaign:
        self._validate_references(payload)
        recipients = self._load_recipients(payload)
        UsageService(self.session, self.tenant_id, self.actor_id).meter(
            EVENT_CAMPAIGN_CREATED,
            resource_type="campaign",
            metadata={"name": payload.name},
        )
        timezone = payload.timezone or payload.timezone_policy or "UTC"
        config = {**payload.schedule_config, "timezone_policy": payload.timezone_policy or timezone, "follow_up_policy": payload.follow_up_policy, "variable_mapping": payload.variable_mapping}
        if payload.scheduled_at is not None:
            config["start_at"] = payload.scheduled_at.isoformat()
        campaign = Campaign(tenant_id=self.tenant_id, name=payload.name, objective=payload.objective, description=payload.description, sender_id=payload.sender_id, template_version_id=payload.template_version_id, recipient_list_id=payload.recipient_list_id, segment_id=payload.segment_id, timezone=timezone, scheduled_at=payload.scheduled_at, status="DRAFT", schedule_config=config, created_by_id=self.actor_id)
        campaign.recipients = [CampaignRecipient(tenant_id=self.tenant_id, contact_id=contact.id) for contact in recipients]
        self.session.add(campaign)
        try:
            self.session.flush()
            self._audit("CAMPAIGN_CREATED", campaign.id, {"name": campaign.name, "recipient_count": len(campaign.recipients)})
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise CampaignError("Campaign recipients or references are invalid") from exc
        return self._campaign(campaign.id)

    def update(self, campaign_id: UUID, payload: CampaignUpdate) -> Campaign:
        campaign = self._campaign(campaign_id)
        if campaign.status in {"SCHEDULED", "RUNNING", "COMPLETED", "CANCELLED"}:
            raise CampaignError("Campaign cannot be edited in its current state")
        self._validate_references(payload)
        values = payload.model_dump(exclude_unset=True)
        material = [key for key in MATERIAL_FIELDS if key in values]
        for attr in ("name", "objective", "description", "sender_id", "template_version_id", "recipient_list_id", "segment_id"):
            if attr in values:
                setattr(campaign, attr, values[attr])
        if "timezone" in values and values["timezone"] is not None:
            campaign.timezone = values["timezone"]
        if "timezone_policy" in values and values["timezone_policy"] is not None:
            campaign.timezone = values["timezone_policy"]
        if "scheduled_at" in values:
            campaign.scheduled_at = values["scheduled_at"]
        config = dict(campaign.schedule_config)
        for key in ("schedule_config", "follow_up_policy", "variable_mapping"):
            if key in values and values[key] is not None:
                config[key] = values[key]
        if "timezone" in values or "timezone_policy" in values:
            config["timezone_policy"] = campaign.timezone
        if "scheduled_at" in values:
            config["start_at"] = campaign.scheduled_at.isoformat() if campaign.scheduled_at else None
        if payload.recipient_ids is not None or payload.recipient_list_id is not None or payload.segment_id is not None:
            recipients = self._load_recipients(payload)
            campaign.recipients.clear()
            campaign.recipients.extend(CampaignRecipient(tenant_id=self.tenant_id, contact_id=contact.id) for contact in recipients)
        campaign.schedule_config = config
        if material:
            self._reversion(campaign)
            if campaign.status != "REVIEW":
                campaign.status = "REVIEW"
        self._audit("CAMPAIGN_UPDATED", campaign.id, {"name": campaign.name, "fields": material, "status": campaign.status})
        self.session.commit()
        return self._campaign(campaign.id)

    def _reversion(self, campaign: Campaign) -> None:
        """Create a fresh (non-immutable) version when an approved history has material edits."""
        if campaign.approved_at is not None:
            campaign.approved_by_id = None
            campaign.approved_at = None
        snapshot = self._snapshot(campaign)
        campaign.versions.append(CampaignVersion(tenant_id=self.tenant_id, version_number=max((item.version_number for item in campaign.versions), default=0) + 1, snapshot=snapshot, is_immutable=False))

    def _snapshot(self, campaign: Campaign) -> dict[str, Any]:
        return {"name": campaign.name, "objective": campaign.objective, "description": campaign.description, "sender_id": str(campaign.sender_id), "template_version_id": str(campaign.template_version_id) if campaign.template_version_id else None, "recipient_list_id": str(campaign.recipient_list_id) if campaign.recipient_list_id else None, "segment_id": str(campaign.segment_id) if campaign.segment_id else None, "timezone": campaign.timezone, "scheduled_at": campaign.scheduled_at.isoformat() if campaign.scheduled_at else None, "schedule_config": campaign.schedule_config, "recipient_ids": [str(item.contact_id) for item in campaign.recipients]}

    def list(self) -> list[Campaign]:
        return list(self.session.scalars(select(Campaign).options(selectinload(Campaign.recipients)).where(Campaign.tenant_id == self.tenant_id).order_by(Campaign.updated_at.desc())).all())

    def validate(self, campaign_id: UUID) -> ValidationResult:
        campaign = self._campaign(campaign_id)
        checks: list[ValidationCheck] = []
        profile = ComplianceProfileService(self.session, self.tenant_id).get()
        sender = self.session.scalar(select(EmailAccount).where(EmailAccount.id == campaign.sender_id, EmailAccount.tenant_id == self.tenant_id))

        # --- Server-side pre-flight (guardrail 11) -------------------------
        # Every gate below is enforced server-side; the UI never substitutes
        # for these checks and direct API/worker calls cannot bypass them.
        checks.append(self._check(
            "sender_available",
            sender is not None and sender.status in {"CONNECTED", "HEALTH_WARNING", "UNKNOWN"},
            "Sender is available and connected",
            "Select a connected sender owned by this tenant.",
            code="CAMPAIGN_BLOCKED_SENDER_AUTH",
        ))
        if sender is not None:
            checks.append(self._check(
                "sender_health",
                sender.status not in {"HEALTH_CRITICAL", "DISABLED", "DISCONNECTED", "SUSPENDED", "REAUTH_REQUIRED"},
                "Sender health is acceptable",
                "Resolve sender health issues before approval.",
                code="CAMPAIGN_BLOCKED_SENDER_HEALTH",
            ))
            domain_health = _validate_domain_health(self.session, self.tenant_id, sender.email)
            checks.append(self._check(
                "sender_domain_auth",
                domain_health is not False,
                "Sender domain authentication is acceptable",
                "Review SPF, DKIM and DMARC configuration for the sender domain.",
                warning=domain_health is None,
                code="CAMPAIGN_BLOCKED_SENDER_AUTH",
            ))
        # Policy acceptance (guardrails 1 & 11): fail closed when the current
        # policy version is required but has not been accepted.
        if profile.require_policy_acceptance:
            from app.services.policies import PolicyService

            policy_service = PolicyService(self.session, self.tenant_id)
            unaccepted = [
                policy_type
                for policy_type in ("terms_of_service", "acceptable_use")
                if not policy_service.is_tenant_accepted(policy_type)
            ]
            checks.append(self._check(
                "tenant_policy_accepted",
                not unaccepted,
                "Tenant has accepted the current Terms of Service and Acceptable Use Policy"
                if not unaccepted
                else f"Required policy acceptance is missing for: {', '.join(unaccepted)}",
                "Accept the current Terms of Service and Acceptable Use Policy.",
                code="CAMPAIGN_BLOCKED_POLICY_ACCEPTANCE",
            ))
        else:
            checks.append(ValidationCheck("tenant_policy_accepted", "PASS", "Policy acceptance is not required by the compliance profile"))

        template = self.session.scalar(select(TemplateVersion).where(TemplateVersion.id == campaign.template_version_id, TemplateVersion.tenant_id == self.tenant_id)) if campaign.template_version_id else None
        checks.append(self._check("template_selected", campaign.template_version_id is not None, "A message template is selected", "Attach a template before approval.", code="CONTENT_REVIEW_REQUIRED"))
        checks.append(self._check("template_active", template is not None and template.status == "ACTIVE", "Template is active", "Use an active template version.", code="CONTENT_REVIEW_REQUIRED"))
        checks.append(self._check("objective_set", bool(campaign.objective and campaign.objective.strip()), "Campaign objective is set", "Describe the campaign objective."))
        raw_recipients = [self.session.get(Contact, item.contact_id) for item in campaign.recipients]
        recipients: list[Contact] = [contact for contact in raw_recipients if contact is not None]
        checks.append(self._check("recipients_present", len(recipients) > 0, "Campaign has recipients", "Add a recipient list or segment.", code="CAMPAIGN_BLOCKED_INVALID_RECIPIENTS"))
        if recipients:
            invalid = [contact for contact in recipients if contact.validation_status in {"INVALID", "DISPOSABLE", "ROLE_ACCOUNT"}]
            checks.append(self._check("recipient_quality", len(invalid) == 0, f"{len(invalid)} recipients are invalid or disposable", "Review recipient validation status.", warning=len(invalid) > 0, code="CAMPAIGN_BLOCKED_INVALID_RECIPIENTS"))
            emails = [contact.email for contact in recipients]
            checks.append(self._check("recipient_suppression", _suppressed_count(self.session, self.tenant_id, emails) == 0, "Recipients pass suppression and unsubscribe checks", "Remove suppressed recipients and do not send to unsubscribed addresses.", code="CAMPAIGN_BLOCKED_SUPPRESSION"))
            # Consent metadata (guardrail 3): when the profile requires recorded
            # consent, recipients with an unknown state fail closed.
            if profile.require_consent_metadata:
                consent_missing = [contact for contact in recipients if contact.consent_status != "SUBSCRIBED"]
                checks.append(self._check(
                    "recipient_consent",
                    len(consent_missing) == 0,
                    f"{len(consent_missing)} recipients have no recorded consent",
                    "Record an explicit consent source and timestamp, or remove those contacts.",
                    code="CAMPAIGN_BLOCKED_CONSENT",
                ))
            else:
                checks.append(ValidationCheck("recipient_consent", "PASS", "Consent metadata is not required by the compliance profile"))
            # Excessive bounce/complaint history on the campaign's sender pool
            # (guardrail 12): a paused sender blocks new campaigns.
            blocked_pool = [
                pool_sender
                for pool_sender in self.session.scalars(
                    select(SenderAccount).join(CampaignSender).where(
                        SenderAccount.tenant_id == self.tenant_id,
                        CampaignSender.campaign_id == campaign.id,
                        CampaignSender.enabled.is_(True),
                        SenderAccount.compliance_status == "REVIEW_REQUIRED",
                    )
                ).all()
            ]
            if blocked_pool:
                checks.append(self._check(
                    "sender_safety_history",
                    not blocked_pool,
                    f"Sender pool is paused for compliance review: {', '.join(s.email for s in blocked_pool)}",
                    "Review the sender's bounce/complaint history and explicitly re-enable it before proceeding.",
                    code="CAMPAIGN_BLOCKED_SENDER_HEALTH",
                ))
        if template is not None and template.variable_manifest:
            mapping = campaign.schedule_config.get("variable_mapping", {}) or {}
            missing = [variable for variable in template.variable_manifest if variable not in mapping]
            checks.append(self._check("variables_mapped", len(missing) == 0, f"Unmapped template variables: {', '.join(missing)}", "Map every template variable.", warning=len(missing) > 0))
        unsubscribe_required = bool(campaign.schedule_config.get("unsubscribe_required", False))
        unsubscribe_present = bool(campaign.schedule_config.get("unsubscribe_url"))
        checks.append(self._check("unsubscribe_mechanism", not unsubscribe_required or unsubscribe_present, "Required unsubscribe mechanism present", "Configure an unsubscribe URL before approval.", code="UNSUBSCRIBE_NOT_CONFIGURED"))
        if profile.require_list_unsubscribe_header:
            checks.append(ValidationCheck("unsubscribe_list_header", "PASS", "List-Unsubscribe headers are added server-side at send time"))
        # Sending limits always present (defaults are seeded); informational.
        checks.append(ValidationCheck("sending_limits", "PASS", "Sending limits are configured"))
        if campaign.scheduled_at is not None and campaign.scheduled_at < datetime.now(UTC):
            checks.append(self._check("schedule_future", False, "Scheduled start is in the past", "Choose a future start time."))
        level: Literal["PASS", "WARNING", "BLOCK"] = "BLOCK" if any(check.outcome == "BLOCK" for check in checks) else "WARNING" if any(check.outcome == "WARNING" for check in checks) else "PASS"
        _persist_compliance_status(campaign, level, checks)
        return ValidationResult(campaign.id, level, tuple(checks))

    def approve(self, campaign_id: UUID) -> Campaign:
        campaign = self._campaign(campaign_id)
        if campaign.status not in {"REVIEW", "DRAFT"}:
            raise CampaignError("Only a campaign in review can be approved")
        result = self.validate(campaign_id)
        blocks = result.blocks
        if blocks:
            details = "; ".join(check.message for check in blocks)
            raise CampaignError(f"Campaign cannot be approved: {details}")
        self._snapshot_recipients(campaign)
        campaign.versions.append(CampaignVersion(tenant_id=self.tenant_id, version_number=max((item.version_number for item in campaign.versions), default=0) + 1, snapshot=self._snapshot(campaign), is_immutable=True))
        campaign.status = "APPROVED"
        campaign.approved_by_id = self.actor_id
        campaign.approved_at = datetime.now(UTC)
        self._audit("CAMPAIGN_APPROVED", campaign.id, {"name": campaign.name, "recipient_count": len(campaign.recipients)})
        self.session.commit()
        return self._campaign(campaign.id)

    def _snapshot_recipients(self, campaign: Campaign) -> None:
        for item in campaign.recipients:
            contact = self.session.get(Contact, item.contact_id)
            if contact is None:
                item.eligibility_status = "SKIPPED"
                continue
            rendered: dict[str, Any] = {field: getattr(contact, field) for field in self._PERSONALIZATION_FIELDS if getattr(contact, field) is not None}
            rendered.update({itemc.field_key: itemc.field_value for itemc in self.session.scalars(select(ContactCustomField).where(ContactCustomField.contact_id == contact.id)).all() if itemc.field_value})
            item.rendered_data = rendered

    def transition(self, campaign_id: UUID, status: str) -> Campaign:
        campaign = self._campaign(campaign_id)
        if status not in self.TRANSITIONS.get(campaign.status, set()):
            raise CampaignError(f"Cannot transition campaign from {campaign.status} to {status}")
        if status == "APPROVED":
            return self.approve(campaign_id)
        if status == "REVIEW" and campaign.approved_at is not None:
            campaign.approved_by_id = None
            campaign.approved_at = None
        if status in {"CANCELLED", "PAUSED", "RESUMED"}:
            self._audit(f"CAMPAIGN_{status}", campaign.id, {"name": campaign.name})
        campaign.status = status
        self.session.commit()
        return self._campaign(campaign.id)

    def duplicate(self, campaign_id: UUID, name: str) -> Campaign:
        source = self._campaign(campaign_id)
        return self.create(CampaignCreate(name=name, objective=source.objective, description=source.description, sender_id=source.sender_id, template_version_id=source.template_version_id, recipient_list_id=source.recipient_list_id, segment_id=source.segment_id, recipient_ids=[item.contact_id for item in source.recipients], schedule_config=source.schedule_config, timezone=source.timezone, timezone_policy=str(source.schedule_config.get("timezone_policy", source.timezone)), follow_up_policy=source.schedule_config.get("follow_up_policy", {}), variable_mapping=source.schedule_config.get("variable_mapping", {})))

    @staticmethod
    def _check(name: str, valid: bool, message: str, remediation: str | None = None, *, warning: bool = False, code: str | None = None) -> ValidationCheck:
        if not valid:
            return ValidationCheck(name, "BLOCK", remediation or message, remediation, code)
        if warning:
            return ValidationCheck(name, "WARNING", message, remediation, code)
        return ValidationCheck(name, "PASS", message, None, code)


def _suppressed_count(session: Session, tenant_id: UUID, emails: list[str]) -> int:
    if not emails:
        return 0
    normalized = [email.strip().lower() for email in emails]
    legacy = session.scalars(select(Suppression.email).where(Suppression.tenant_id == tenant_id, Suppression.email.in_(emails))).all()
    modern = session.scalars(select(SuppressionEntry.email_normalized).where(SuppressionEntry.tenant_id == tenant_id, SuppressionEntry.active.is_(True), SuppressionEntry.email_normalized.in_(normalized))).all()
    unsubscribed = session.scalars(select(Unsubscribe.email).where(Unsubscribe.tenant_id == tenant_id, Unsubscribe.email.in_(emails))).all()
    return len(set(legacy) | set(modern) | set(unsubscribed))


def _validate_domain_health(session: Session, tenant_id: UUID, email: str) -> bool | None:
    """None = no domain record (unknown/not-yet-verified), False = failing, True = acceptable."""
    if not email or "@" not in email:
        return None
    record = session.scalar(
        select(Domain).where(
            Domain.tenant_id == tenant_id,
            Domain.domain == email.rsplit("@", 1)[1].lower(),
        )
    )
    if record is None:
        return None
    if record.health_status in {"FAIL", "CRITICAL"}:
        return False
    if record.health_status in {"WARNING", "UNKNOWN"}:
        return None
    return True


def _persist_compliance_status(campaign: Campaign, level: str, checks: list[ValidationCheck]) -> None:
    codes = [check.code for check in checks if check.code]
    from app.services.compliance_status import (
        BLOCKED,
        COMPLIANT,
        WARNING,
        normalize_reason,
    )

    campaign.compliance_status = BLOCKED if level == "BLOCK" else WARNING if level == "WARNING" else COMPLIANT
    campaign.compliance_reasons = normalize_reason(codes)
    campaign.compliance_evaluated_at = datetime.now(UTC)
