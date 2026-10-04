"""Automatic protective pausing of senders (guardrail 12).

When a sender or campaign exceeds configured risk thresholds the platform
automatically pauses new campaign sends and requires human review. Thresholds
live in the tenant's ``ComplianceProfile.safety_thresholds`` and are validated
for sane ranges (never presented as universal legal limits).

A sender that trips a serious provider/compliance failure is never resumed
automatically: ``release`` is the only path forward and is an explicit,
audited human action.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import ComplianceProfile, Message, MessageEvent, SenderAccount
from app.services.audit import AuditService
from app.services.compliance_profile import ComplianceProfileService

# Machine-readable reasons (guardrail 23) used consistently by workers, API,
# and frontend.
HIGH_BOUNCE_RATE = "HIGH_BOUNCE_RATE"
HIGH_COMPLAINT_RATE = "HIGH_COMPLAINT_RATE"
PROVIDER_ERROR_RATE = "PROVIDER_POLICY_BLOCK"
SUSPICIOUS_ACTIVITY = "SUSPICIOUS_ACTIVITY"

_REVIEW_REASONS = {HIGH_BOUNCE_RATE, HIGH_COMPLAINT_RATE, PROVIDER_ERROR_RATE}


class SenderSafetyError(ValueError):
    pass


class SenderSafetyService:
    """Evaluates and persists the safety/compliance state of sender accounts."""

    def __init__(self, session: Session, tenant_id: UUID, actor_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id

    # ------------------------------------------------------------------ #
    # Pure evaluation
    # ------------------------------------------------------------------ #
    def evaluate(self, account: SenderAccount, profile: ComplianceProfile | None = None) -> tuple[str, list[str]]:
        """Determine the sender's compliance state from send telemetry.

        Returns ``(state, reasons)`` where state is COMPLIANT | WARNING |
        REVIEW_REQUIRED. Evaluation is fail-detect: with too little telemetry
        it reports COMPLIANT (no evidence of a problem), but any tripped
        threshold immediately requires review.
        """
        profile = profile or ComplianceProfileService(self.session, self.tenant_id).get()
        thresholds = ComplianceProfileService.thresholds(profile)
        window_days = int(thresholds.get("window_days", 7))
        min_sample = int(thresholds.get("min_sample_size", 30))
        hard_bounce_rate = float(thresholds.get("hard_bounce_rate", 0.05))
        complaint_rate = float(thresholds.get("complaint_rate", 0.001))
        provider_error_rate = float(thresholds.get("provider_error_rate", 0.10))

        since = datetime.now(UTC) - timedelta(days=window_days)

        sent = self.session.scalar(
            select(func.count(Message.id)).where(
                Message.tenant_id == self.tenant_id,
                Message.sender_account_id == account.id,
                Message.created_at >= since,
                Message.msg_type == "CAMPAIGN",
            )
        ) or 0
        if sent < min_sample:
            return "COMPLIANT", []

        events = self.session.execute(
            select(
                MessageEvent.event_type,
                func.count(MessageEvent.id),
            )
            .join(Message, Message.id == MessageEvent.message_id)
            .where(
                Message.tenant_id == self.tenant_id,
                Message.sender_account_id == account.id,
                MessageEvent.occurred_at >= since,
                MessageEvent.event_type.in_(
                    ["HARD_BOUNCE", "COMPLAINT", "TEMPORARY_FAILURE"]
                ),
            )
            .group_by(MessageEvent.event_type)
        ).all()
        counts = {event_type: int(count) for event_type, count in events}

        reasons: list[str] = []
        hard_bounced = counts.get("HARD_BOUNCE", 0)
        complained = counts.get("COMPLAINT", 0)
        provider_errors = counts.get("TEMPORARY_FAILURE", 0)

        if (hard_bounced / sent) >= hard_bounce_rate:
            reasons.append(HIGH_BOUNCE_RATE)
        if (complained / sent) >= complaint_rate:
            reasons.append(HIGH_COMPLAINT_RATE)
        if (provider_errors / sent) >= provider_error_rate:
            reasons.append(PROVIDER_ERROR_RATE)

        if reasons:
            return "REVIEW_REQUIRED", reasons
        if hard_bounced or complained or provider_errors:
            return "WARNING", []
        return "COMPLIANT", []

    def evaluate_by_account(self, account: SenderAccount) -> tuple[str, list[str]]:
        profile = ComplianceProfileService(self.session, self.tenant_id).get()
        return self.evaluate(account, profile)

    # ------------------------------------------------------------------ #
    # Persisting
    # ------------------------------------------------------------------ #
    def apply(self, account: SenderAccount, state: str, reasons: list[str]) -> SenderAccount:
        """Persist the evaluated state and pause campaign traffic on a trip.

        Paused senders (REVIEW_REQUIRED) are never downgraded or auto-resumed
        here — re-evaluation of a paused sender is a no-op until a human
        explicitly calls ``release`` (guardrail 12).
        """
        account.compliance_evaluated_at = datetime.now(UTC)
        if account.compliance_status == "REVIEW_REQUIRED":
            return account
        prior = account.compliance_status
        account.compliance_status = state
        account.compliance_reasons = reasons
        if state == "REVIEW_REQUIRED" and any(
            reason in _REVIEW_REASONS for reason in reasons
        ):
            account.paused_at = datetime.now(UTC)
            account.resume_guard = True
            AuditService(self.session, self.tenant_id, self.actor_id).record(
                "SENDER_PAUSED",
                "sender_account",
                account.id,
                {
                    "email": account.email,
                    "reasons": reasons,
                    "from": prior,
                },
            )
        elif state == "COMPLIANT" and prior == "WARNING":
            account.paused_at = None
            account.resume_guard = False
        self.session.flush()
        return account

    def release(self, account_id: UUID, *, review_note: str | None = None) -> SenderAccount:
        """Explicit, audited, human-only re-enable of a paused sender."""
        account = self.session.scalar(
            select(SenderAccount).where(
                SenderAccount.id == account_id,
                SenderAccount.tenant_id == self.tenant_id,
            )
        )
        if account is None:
            raise SenderSafetyError("Sender account not found")
        if account.compliance_status != "REVIEW_REQUIRED":
            raise SenderSafetyError("Sender is not paused for review")
        account.compliance_status = "COMPLIANT"
        account.compliance_reasons = []
        account.resume_guard = False
        account.paused_at = None
        account.compliance_evaluated_at = datetime.now(UTC)
        AuditService(self.session, self.tenant_id, self.actor_id).record(
            "SENDER_RE_ENABLED",
            "sender_account",
            account.id,
            {"email": account.email, "review_note": review_note or ""},
        )
        self.session.flush()
        self.session.commit()
        return account