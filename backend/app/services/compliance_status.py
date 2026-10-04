"""Normalized compliance state for senders/campaigns (guardrail 23).

States are COMPLIANT | WARNING | BLOCKED | REVIEW_REQUIRED with machine-readable
reasons (UNSUBSCRIBE_NOT_CONFIGURED, SUPPRESSION_CHECK_FAILED, HIGH_BOUNCE_RATE,
HIGH_COMPLAINT_RATE, SENDER_AUTHENTICATION_REQUIRED, PROVIDER_POLICY_BLOCK,
TENANT_POLICY_NOT_ACCEPTED, SENDER_NOT_AUTHENTICATED, ...). The same enum is
consumed by the API, workers, and the frontend so a single status vocabulary
drives the whole product.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.models import Campaign, EmailAccount, SenderAccount

COMPLIANT = "COMPLIANT"
WARNING = "WARNING"
BLOCKED = "BLOCKED"
REVIEW_REQUIRED = "REVIEW_REQUIRED"
UNKNOWN = "UNKNOWN"

COMPLIANCE_STATES = (COMPLIANT, WARNING, BLOCKED, REVIEW_REQUIRED, UNKNOWN)

# Machine-readable reason codes (guardrail 23) surfaced across API/workers/UI.
REASONS = {
    "UNSUBSCRIBE_NOT_CONFIGURED": "Required unsubscribe mechanism is not configured",
    "UNSUBSCRIBE_LIST_HEADER": "List-Unsubscribe header support is required",
    "SUPPRESSION_CHECK_FAILED": "Recipients include suppressed or unsubscribed addresses",
    "HIGH_BOUNCE_RATE": "Hard bounce rate exceeds the configured safety threshold",
    "HIGH_COMPLAINT_RATE": "Complaint rate exceeds the configured safety threshold",
    "PROVIDER_POLICY_BLOCK": "Provider policy or repeated provider errors require review",
    "SENDER_AUTHENTICATION_REQUIRED": "Sender authentication or re-authentication is required",
    "SENDER_NOT_AUTHENTICATED": "Sender is not authenticated",
    "SENDER_DISABLED": "Sender is disabled",
    "SENDER_HEALTH_CRITICAL": "Sender health is critical",
    "TENANT_POLICY_NOT_ACCEPTED": "The tenant has not accepted the current policy version",
    "CONSENT_METADATA_REQUIRED": "Configured recipients lack recorded consent metadata",
    "CONTENT_REVIEW_REQUIRED": "Campaign content requires human review",
    "CAMPAIGN_NOT_APPROVED": "Campaign has not completed human approval",
    "INVALID_RECIPIENTS": "Campaign includes invalid or ineligible recipients",
    "RATE_LIMIT": "A rate or usage limit has been reached",
}

# Maps campaign pre-flight block/warning codes onto the normalized reason set.
_VALIDATE_CODE_TO_REASON = {
    "CAMPAIGN_BLOCKED_POLICY_ACCEPTANCE": "TENANT_POLICY_NOT_ACCEPTED",
    "CAMPAIGN_BLOCKED_CONSENT": "CONSENT_METADATA_REQUIRED",
    "CAMPAIGN_BLOCKED_SENDER_AUTH": "SENDER_AUTHENTICATION_REQUIRED",
    "CAMPAIGN_BLOCKED_SENDER_HEALTH": "SENDER_HEALTH_CRITICAL",
    "CAMPAIGN_BLOCKED_SUPPRESSION": "SUPPRESSION_CHECK_FAILED",
    "CAMPAIGN_BLOCKED_INVALID_RECIPIENTS": "INVALID_RECIPIENTS",
    "CAMPAIGN_BLOCKED_RATE_LIMIT": "RATE_LIMIT",
    "UNSUBSCRIBE_NOT_CONFIGURED": "UNSUBSCRIBE_NOT_CONFIGURED",
}


def normalize_reason(codes: list[str] | None) -> list[str]:
    """Translate arbitrary pre-flight codes into the shared reason vocabulary."""
    result: list[str] = []
    for code in codes or []:
        reason = _VALIDATE_CODE_TO_REASON.get(code, code)
        if reason not in result:
            result.append(reason)
    return result


class ComplianceStatusService:
    """Computes and persists the normalized compliance state of a sender or campaign."""

    def __init__(self, session: Session, tenant_id: UUID, actor_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id

    # ------------------------------------------------------------------ #
    # Campaigns
    # ------------------------------------------------------------------ #
    def campaign_status(self, campaign: Campaign) -> tuple[str, list[str]]:
        from app.models import CampaignSender
        from app.services.campaigns import CampaignService

        result = CampaignService(self.session, self.tenant_id, self.actor_id).validate(
            campaign.id
        )
        codes = [check.code for check in result.checks if check.code]
        reasons = normalize_reason(codes)

        if result.blocks:
            state = BLOCKED
        elif result.level == "WARNING":
            state = WARNING
        else:
            state = COMPLIANT

        # Paused senders in the campaign's enabled pool keep the campaign in
        # REVIEW_REQUIRED until a human explicitly re-enables them (guardrail 12).
        paused_pool = list(
            self.session.scalars(
                select(SenderAccount)
                .join(CampaignSender)
                .where(
                    SenderAccount.tenant_id == self.tenant_id,
                    CampaignSender.campaign_id == campaign.id,
                    CampaignSender.enabled.is_(True),
                    SenderAccount.compliance_status == "REVIEW_REQUIRED",
                )
            ).all()
        )
        if paused_pool:
            state = REVIEW_REQUIRED
            for sender in paused_pool:
                for reason in sender.compliance_reasons or []:
                    if reason not in reasons:
                        reasons.append(reason)
        campaign.compliance_status = state
        campaign.compliance_reasons = reasons
        campaign.compliance_evaluated_at = datetime.now(UTC)
        return state, reasons

    def persist_campaign(self, campaign: Campaign) -> Campaign:
        self.campaign_status(campaign)
        self.session.flush()
        return campaign

    # ------------------------------------------------------------------ #
    # Senders
    # ------------------------------------------------------------------ #
    def sender_status(self, sender: SenderAccount | EmailAccount) -> tuple[str, list[str]]:
        if isinstance(sender, EmailAccount):
            return self._legacy_sender_status(sender)
        return self._system_b_sender_status(sender)

    def _system_b_sender_status(self, sender: SenderAccount) -> tuple[str, list[str]]:
        from app.services.compliance_profile import ComplianceProfileService
        from app.services.safety import SenderSafetyService

        profile = ComplianceProfileService(self.session, self.tenant_id).get()
        safety_state, safety_reasons = SenderSafetyService(
            self.session, self.tenant_id, self.actor_id
        ).evaluate(sender, profile)

        if sender.status != "ACTIVE":
            return BLOCKED, ["SENDER_NOT_AUTHENTICATED"]
        if sender.health_status in {"FAILED", "REAUTH_REQUIRED"}:
            return BLOCKED, ["SENDER_AUTHENTICATION_REQUIRED"]
        if sender.health_status == "DEGRADED":
            return WARNING, ["SENDER_AUTHENTICATION_REQUIRED"]
        if safety_state == REVIEW_REQUIRED:
            return REVIEW_REQUIRED, safety_reasons
        if safety_state == WARNING:
            return WARNING, []
        return COMPLIANT, []

    def _legacy_sender_status(self, sender: EmailAccount) -> tuple[str, list[str]]:
        if sender.status in {"HEALTH_CRITICAL", "DISABLED", "SUSPENDED"}:
            return BLOCKED, ["SENDER_HEALTH_CRITICAL"]
        if sender.status in {"DISCONNECTED", "REAUTH_REQUIRED"}:
            return BLOCKED, ["SENDER_AUTHENTICATION_REQUIRED"]
        if sender.status == "HEALTH_WARNING":
            return WARNING, []
        return COMPLIANT, []

    # ------------------------------------------------------------------ #
    # Persisting sender state
    # ------------------------------------------------------------------ #
    def apply_sender(self, sender: SenderAccount) -> SenderAccount:
        state, reasons = self.sender_status(sender)
        from app.services.safety import SenderSafetyService

        SenderSafetyService(self.session, self.tenant_id, self.actor_id).apply(
            sender, state, reasons
        )
        return sender