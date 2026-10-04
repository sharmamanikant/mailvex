from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import (
    Campaign,
    CampaignRecipient,
    Contact,
    Domain,
    EmailAccount,
    Suppression,
    SuppressionEntry,
    TemplateVariable,
    TemplateVersion,
    Unsubscribe,
)
from app.models import (
    ComplianceResult as ComplianceResultRecord,
)
from app.services.audit import AuditService
from app.services.policy import PolicyEngine

Outcome = Literal["PASS", "WARNING", "BLOCK"]

_CHECK_ORDER = (
    "campaign_approved",
    "sender_authorized",
    "sender_connected",
    "recipient_valid",
    "suppression",
    "unsubscribe",
    "bounce_status",
    "complaint_status",
    "personalization_data",
    "domain_health",
    "sender_health",
    "provider_capacity",
    "campaign_policy",
    "tenant_policy",
    "tenant_policy_accepted",
    "consent_metadata",
)

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

# Senders in these states cannot send; they surface as both a sender_health
# failure and an unavailable provider (DISCONNECTED / SUSPENDED are Phase 10).
_SENDER_UNAVAILABLE_STATUSES = frozenset({"DISABLED", "HEALTH_CRITICAL", "REAUTH_REQUIRED", "DISCONNECTED", "SUSPENDED"})


@dataclass(frozen=True)
class ComplianceCheck:
    name: str
    outcome: Outcome
    message: str
    remediation: str | None = None


@dataclass(frozen=True)
class ComplianceResult:
    campaign_id: UUID
    recipient_id: UUID | None
    outcome: Outcome
    checks: tuple[ComplianceCheck, ...] = field(default_factory=tuple)

    @property
    def passed(self) -> tuple[ComplianceCheck, ...]:
        return tuple(check for check in self.checks if check.outcome == "PASS")

    @property
    def warnings(self) -> tuple[ComplianceCheck, ...]:
        return tuple(check for check in self.checks if check.outcome == "WARNING")

    @property
    def failures(self) -> tuple[ComplianceCheck, ...]:
        return tuple(check for check in self.checks if check.outcome == "BLOCK")


class ComplianceService:
    """Mandatory send-time policy gate.

    Every future outgoing message MUST pass this service. No provider adapter
    may bypass it. Each check is evaluated at send time (not just campaign
    creation), persisted to ``compliance_results``, and BLOCK/WARNING outcomes
    emit audit events.
    """

    def __init__(
        self,
        session: Session,
        tenant_id: UUID,
        actor_id: UUID | None = None,
        check_source: str = "SEND",
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id
        self.check_source = check_source

    def evaluate(
        self,
        campaign: Campaign,
        sender: EmailAccount | None,
        recipient: Contact | None,
        recipient_link: CampaignRecipient | None = None,
    ) -> ComplianceResult:
        checks: list[ComplianceCheck] = []

        checks.append(
            self._check(
                "campaign_approved",
                campaign.status in {"APPROVED", "SCHEDULED", "RUNNING"},
                "Campaign is approved",
                "Move the campaign through human review and approval.",
            )
        )

        checks.append(
            self._check(
                "sender_authorized",
                sender is not None and sender.tenant_id == self.tenant_id,
                "Sender is authorized",
                "Select a sender owned by this tenant.",
            )
        )

        checks.append(
            self._check(
                "sender_connected",
                sender is not None and not self._is_unavailable(sender),
                "Sender is connected",
                "Reconnect or enable the sender.",
            )
        )

        email = recipient.email if recipient else ""
        checks.append(
            self._check(
                "recipient_valid",
                recipient is not None
                and recipient.tenant_id == self.tenant_id
                and recipient.validation_status not in {"INVALID", "DISPOSABLE", "ROLE_ACCOUNT"},
                "Recipient is valid",
                "Validate the recipient before sending.",
            )
        )

        normalized_email = email.strip().lower()
        suppressed = self.session.scalar(
            select(Suppression.id).where(
                Suppression.tenant_id == self.tenant_id,
                Suppression.email == normalized_email,
            )
        ) is not None
        if not suppressed:
            suppressed = self.session.scalar(
                select(SuppressionEntry.id).where(
                    SuppressionEntry.tenant_id == self.tenant_id,
                    SuppressionEntry.email_normalized == normalized_email,
                    SuppressionEntry.active.is_(True),
                )
            ) is not None
        checks.append(
            self._check(
                "suppression",
                not suppressed,
                "Recipient is not suppressed",
                "Remove the recipient from the campaign or review suppression.",
            )
        )

        unsubscribed = self.session.scalar(
            select(Unsubscribe.id).where(
                Unsubscribe.tenant_id == self.tenant_id,
                Unsubscribe.email == email.strip().lower(),
            )
        ) is not None
        checks.append(
            self._check(
                "unsubscribe",
                not unsubscribed,
                "Recipient is not unsubscribed",
                "Do not send to unsubscribed recipients.",
            )
        )

        hard_bounced = self.session.scalar(
            select(Suppression.id).where(
                Suppression.tenant_id == self.tenant_id,
                Suppression.email == email.strip().lower(),
                Suppression.reason == "HARD_BOUNCE",
            )
        ) is not None
        checks.append(
            self._check(
                "bounce_status",
                not hard_bounced,
                "Recipient has not hard bounced",
                "Do not send to a recipient that previously hard bounced.",
            )
        )

        complained = self.session.scalar(
            select(Suppression.id).where(
                Suppression.tenant_id == self.tenant_id,
                Suppression.email == email.strip().lower(),
                Suppression.reason == "COMPLAINT",
            )
        ) is not None
        checks.append(
            self._check(
                "complaint_status",
                not complained,
                "Recipient has not filed a complaint",
                "Do not send to a recipient that previously filed a spam complaint.",
            )
        )

        checks.append(self._personalization_check(campaign, recipient, recipient_link))

        domain = self._domain(sender.email if sender else "")
        checks.append(
            self._check(
                "domain_health",
                domain is None or domain.health_status not in {"FAIL", "CRITICAL"},
                "Domain health is acceptable",
                "Review SPF, DKIM, DMARC, MX, and TLS configuration.",
                warning=domain is not None and domain.health_status in {"WARNING", "UNKNOWN"},
            )
        )

        checks.append(
            self._check(
                "sender_health",
                sender is not None and sender.status not in _SENDER_UNAVAILABLE_STATUSES,
                "Sender health is acceptable",
                "Resolve sender health issues before sending.",
            )
        )

        checks.append(
            self._check(
                "provider_capacity",
                sender is not None and not self._is_unavailable(sender),
                "Provider capacity is available",
                "Defer this message according to provider policy.",
            )
        )

        unsubscribe_required = bool(campaign.schedule_config.get("unsubscribe_required", False))
        unsubscribe_present = bool(campaign.schedule_config.get("unsubscribe_url"))
        checks.append(
            self._check(
                "unsubscribe_mechanism",
                not unsubscribe_required or unsubscribe_present,
                "Required unsubscribe mechanism is present",
                "Configure an unsubscribe URL before sending.",
            )
        )

        checks.append(
            self._check(
                "campaign_policy",
                self._campaign_policy_ok(campaign),
                "Campaign policy is satisfied",
                "Review the campaign's sending policy.",
            )
        )

        tenant_policy_ok, tenant_policy_message, tenant_policy_remediation = self._tenant_policy(campaign)
        checks.append(
            self._check(
                "tenant_policy",
                tenant_policy_ok,
                tenant_policy_message,
                tenant_policy_remediation,
            )
        )

        checks.append(
            self._check(
                "tenant_policy_accepted",
                self._tenant_policy_accepted(),
                "Required policy versions are accepted",
                "Accept the current Terms of Service and Acceptable Use Policy.",
            )
        )

        consent_required, consent_partial = self._consent_state(recipient)
        checks.append(
            self._check(
                "consent_metadata",
                not consent_required or consent_partial == "SUBSCRIBED",
                "Recipient consent is recorded and current"
                if consent_required and consent_partial == "SUBSCRIBED"
                else "Recipient consent is not required"
                if not consent_required
                else "Recipient has no recorded consent",
                "Record an explicit consent source and timestamp before sending.",
                warning=consent_required and consent_partial not in (None, "SUBSCRIBED"),
            )
        )

        outcome: Outcome = (
            "BLOCK"
            if any(check.outcome == "BLOCK" for check in checks)
            else "WARNING"
            if any(check.outcome == "WARNING" for check in checks)
            else "PASS"
        )
        checks_ordered: list[ComplianceCheck] = []
        seen: set[str] = set()
        for name in _CHECK_ORDER:
            for check in checks:
                if check.name == name and name not in seen:
                    checks_ordered.append(check)
                    seen.add(name)
        for check in checks:
            if check.name not in seen:
                checks_ordered.append(check)
                seen.add(check.name)

        self._persist(campaign, recipient_link, sender, checks_ordered)
        self._audit(outcome, campaign.id, checks_ordered)
        return ComplianceResult(
            campaign.id,
            recipient_link.id if recipient_link is not None else None,
            outcome,
            tuple(checks_ordered),
        )

    def check_recipient(
        self,
        campaign: Campaign,
        sender: EmailAccount,
        recipient: Contact,
        recipient_link: CampaignRecipient,
    ) -> ComplianceResult:
        """Immediately-before-delivery safety recheck (Phase 14).

        Verifies suppression (modern + legacy), unsubscribe, bounce, complaint,
        campaign status, and sender status. On a BLOCK the recipient is marked
        SUPPRESSED and must not be retried. This is the gate that guards the
        final handoff to a provider adapter.
        """
        result = self.evaluate(campaign, sender, recipient, recipient_link)
        if result.outcome == "BLOCK":
            recipient_link.eligibility_status = "SUPPRESSED"
            self.session.flush()
        return result

    def _persist(
        self,
        campaign: Campaign,
        recipient_link: CampaignRecipient | None,
        sender: EmailAccount | None,
        checks: list[ComplianceCheck],
    ) -> None:
        for check in checks:
            self.session.add(
                ComplianceResultRecord(
                    tenant_id=self.tenant_id,
                    campaign_id=campaign.id,
                    recipient_id=recipient_link.id if recipient_link is not None else None,
                    sender_id=sender.id if sender is not None else None,
                    check_type=check.name,
                    result=check.outcome,
                    reason=check.message,
                    check_source=self.check_source,
                )
            )
        self.session.flush()

    def _audit(self, outcome: Outcome, campaign_id: UUID, checks: list[ComplianceCheck]) -> None:
        if outcome == "BLOCK":
            AuditService(self.session, self.tenant_id, self.actor_id).record(
                "COMPLIANCE_BLOCK",
                "campaign",
                campaign_id,
                {
                    "blocks": [
                        {"check": check.name, "reason": check.message}
                        for check in checks
                        if check.outcome == "BLOCK"
                    ]
                },
            )
        elif outcome == "WARNING":
            AuditService(self.session, self.tenant_id, self.actor_id).record(
                "COMPLIANCE_WARNING",
                "campaign",
                campaign_id,
                {
                    "warnings": [
                        {"check": check.name, "reason": check.message}
                        for check in checks
                        if check.outcome == "WARNING"
                    ]
                },
            )

    def _domain(self, email: str) -> Domain | None:
        if "@" not in email:
            return None
        return self.session.scalar(
            select(Domain).where(
                Domain.tenant_id == self.tenant_id,
                Domain.domain == email.rsplit("@", 1)[1].lower(),
            )
        )

    def _is_unavailable(self, sender: EmailAccount) -> bool:
        return sender.status in _SENDER_UNAVAILABLE_STATUSES

    def _personalization_check(
        self,
        campaign: Campaign,
        recipient: Contact | None,
        recipient_link: CampaignRecipient | None,
    ) -> ComplianceCheck:
        template = (
            self.session.scalar(
                select(TemplateVersion).where(
                    TemplateVersion.id == campaign.template_version_id,
                    TemplateVersion.tenant_id == self.tenant_id,
                )
            )
            if campaign.template_version_id
            else None
        )
        if template is None:
            return ComplianceCheck(
                "personalization_data",
                "PASS",
                "No template selected; personalization not evaluated",
            )
        declared_required: set[str] = set(
            self.session.scalars(
                select(TemplateVariable.name).where(
                    TemplateVariable.tenant_id == self.tenant_id,
                    TemplateVariable.template_version_id == template.id,
                    TemplateVariable.required.is_(True),
                )
            ).all()
        )
        manifest = set(template.variable_manifest or [])
        known = set(_PERSONALIZATION_FIELDS)
        required_vars = (declared_required | manifest) & known
        if not required_vars:
            return ComplianceCheck("personalization_data", "PASS", "Required personalization data is present")
        data: dict[str, Any] = dict(recipient_link.rendered_data) if recipient_link is not None else {}
        if recipient is not None:
            for field_name in _PERSONALIZATION_FIELDS:
                value = getattr(recipient, field_name, None)
                if value is not None and value not in ("",):
                    data.setdefault(field_name, value)
        missing = sorted(
            variable for variable in required_vars if (data.get(variable) is None or data.get(variable) == "")
        )
        if not missing:
            return ComplianceCheck("personalization_data", "PASS", "Required personalization data is present")
        display = ", ".join(missing)
        return ComplianceCheck(
            "personalization_data",
            "WARNING",
            f"Required personalization is missing for: {display}",
            f"Populate the missing field(s): {display}",
        )

    def _tenant_policy(self, campaign: Campaign) -> tuple[bool, str, str | None]:
        legacy = campaign.schedule_config.get("tenant_policy", {})
        legacy_allowed = not isinstance(legacy, dict) or legacy.get("sending_enabled", True) is not False
        policy_checks = PolicyEngine(self.session, self.tenant_id).evaluate_tenant(campaign)
        blocks = [check for check in policy_checks if check.outcome == "BLOCK"]
        if not legacy_allowed or blocks:
            messages = "; ".join(check.message for check in blocks)
            if not blocks:
                messages = "Tenant sending is disabled by policy"
            remediations = "; ".join(
                check.remediation or "Resolve the policy conflict." for check in blocks
            ) or "Enable sending in the tenant policy."
            return False, messages or "Tenant sending policy permits this message", remediations
        return True, "Tenant sending policy permits this message", None

    def _campaign_policy_ok(self, campaign: Campaign) -> bool:
        policy_checks = PolicyEngine(self.session, self.tenant_id).evaluate_campaign(campaign)
        return all(check.outcome != "BLOCK" for check in policy_checks)

    def _tenant_policy_accepted(self) -> bool:
        """Guardrail 1/11: fail closed if current policies are unaccepted."""
        from app.services.compliance_profile import ComplianceProfileService
        from app.services.policies import PolicyService

        profile = ComplianceProfileService(self.session, self.tenant_id).get()
        if not profile.require_policy_acceptance:
            return True
        policy_service = PolicyService(self.session, self.tenant_id)
        return all(
            policy_service.is_tenant_accepted(policy_type)
            for policy_type in ("terms_of_service", "acceptable_use")
        )

    def _consent_state(self, recipient: Contact | None) -> tuple[bool, str | None]:
        """Guardrail 3: returns (required, recipient_consent_status)."""
        from app.services.compliance_profile import ComplianceProfileService

        required = ComplianceProfileService(self.session, self.tenant_id).get().require_consent_metadata
        if not required:
            return False, None
        return True, recipient.consent_status if recipient is not None else None

    @staticmethod
    def _check(
        name: str,
        valid: bool,
        message: str,
        remediation: str | None,
        *,
        warning: bool = False,
    ) -> ComplianceCheck:
        if not valid:
            return ComplianceCheck(name, "BLOCK", remediation or message, remediation)
        if warning:
            return ComplianceCheck(name, "WARNING", message, remediation)
        return ComplianceCheck(name, "PASS", message, None)
