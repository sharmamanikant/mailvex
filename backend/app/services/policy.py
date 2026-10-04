from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import Campaign, EmailAccount, Message, Tenant

Outcome = Literal["PASS", "WARNING", "BLOCK"]


@dataclass(frozen=True)
class PolicyCheck:
    """A single policy evaluation returned by the policy engine.

    None of the values are hard-coded; every bound is read from the tenant
    policy or the campaign's own policy overrides.
    """

    code: str
    outcome: Outcome
    message: str
    remediation: str | None = None


class PolicyEngine:
    """Configurable tenant + campaign policy gate.

    Divergence from hard-coding: the policy VALUES are read from
    ``Tenant.policy_defaults`` merged with the campaign's
    ``schedule_config["policy"]`` overrides. Provider adapters never embed
    these limits.
    """

    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    @staticmethod
    def effective_policy(tenant_defaults: dict[str, Any], campaign_overrides: dict[str, Any]) -> dict[str, Any]:
        merged: dict[str, Any] = dict(tenant_defaults or {})
        merged.update(campaign_overrides or {})
        return merged

    def _tenant(self) -> Tenant | None:
        return self.session.get(Tenant, self.tenant_id)

    def _campaign_policy(self, campaign: Campaign) -> dict[str, Any]:
        tenant = self._tenant()
        tenant_defaults = dict(tenant.policy_defaults) if tenant is not None else {}
        overrides = campaign.schedule_config.get("policy", {})
        if not isinstance(overrides, dict):
            overrides = {}
        return self.effective_policy(tenant_defaults, overrides)

    def evaluate_campaign(self, campaign: Campaign) -> list[PolicyCheck]:
        """Policy checks that depend on the campaign definition (size, schedule)."""
        policy = self._campaign_policy(campaign)
        checks: list[PolicyCheck] = []

        max_size = policy.get("maximum_campaign_size")
        if isinstance(max_size, int) and max_size > 0:
            size = len(campaign.recipients)
            if size > max_size:
                checks.append(
                    PolicyCheck(
                        "maximum_campaign_size",
                        "BLOCK",
                        f"Campaign has {size} recipients which exceeds the maximum of {max_size}",
                        "Reduce the recipient list or raise the tenant policy limit.",
                    )
                )

        allowed_hours = policy.get("allowed_sending_hours")
        if isinstance(allowed_hours, list) and allowed_hours:
            hour = datetime.now(UTC).hour
            if hour not in [int(value) for value in allowed_hours if isinstance(value, (int, float))]:
                checks.append(
                    PolicyCheck(
                        "allowed_sending_hours",
                        "BLOCK",
                        f"Current hour ({hour:02d}:00 UTC) is outside the allowed sending window",
                        "Retry within the tenant's allowed sending hours.",
                    )
                )

        allowed_weekdays = policy.get("allowed_weekdays")
        if isinstance(allowed_weekdays, list) and allowed_weekdays:
            weekday = datetime.now(UTC).weekday()
            if weekday not in [int(value) for value in allowed_weekdays if isinstance(value, (int, float))]:
                checks.append(
                    PolicyCheck(
                        "allowed_weekdays",
                        "BLOCK",
                        f"Today (weekday {weekday}) is outside the allowed sending days",
                        "Retry on an allowed weekday.",
                    )
                )
        return checks

    def evaluate_tenant(self, campaign: Campaign) -> list[PolicyCheck]:
        """Policy checks scoped to the tenant and its configured limits."""
        policy = self._campaign_policy(campaign)
        checks: list[PolicyCheck] = []

        require_approval = policy.get("require_approval", False)
        if require_approval and campaign.status not in {"APPROVED", "SCHEDULED", "RUNNING"}:
            checks.append(
                PolicyCheck(
                    "require_approval",
                    "BLOCK",
                    "Tenant policy requires human approval before sending",
                    "Approve the campaign before scheduling.",
                )
            )

        require_unsubscribe = policy.get("require_unsubscribe", False)
        if require_unsubscribe and not campaign.schedule_config.get("unsubscribe_url"):
            checks.append(
                PolicyCheck(
                    "require_unsubscribe",
                    "BLOCK",
                    "Tenant policy requires an unsubscribe mechanism",
                    "Configure an unsubscribe URL before sending.",
                )
            )

        max_daily = policy.get("maximum_daily_messages")
        if isinstance(max_daily, int) and max_daily > 0:
            since = datetime.now(UTC) - timedelta(days=1)
            sent_today = self.session.scalar(
                select(func.count(Message.id)).where(
                    Message.tenant_id == self.tenant_id,
                    Message.created_at >= since,
                )
            ) or 0
            if sent_today >= max_daily:
                checks.append(
                    PolicyCheck(
                        "maximum_daily_messages",
                        "BLOCK",
                        f"Daily message limit of {max_daily} has been reached",
                        "Resume sending tomorrow or raise the tenant daily limit.",
                    )
                )

        restrictions = policy.get("sender_restrictions")
        if isinstance(restrictions, list) and restrictions:
            sender = self.session.get(EmailAccount, campaign.sender_id)
            allowed = [str(value).lower() for value in restrictions]
            if sender is not None and sender.email.lower() not in allowed:
                checks.append(
                    PolicyCheck(
                        "sender_restrictions",
                        "BLOCK",
                        f"Sender {sender.email} is not on the allowed sender list",
                        "Choose an allowed sender or update the policy.",
                    )
                )

        return checks

    def evaluate(self, campaign: Campaign) -> list[PolicyCheck]:
        return self.evaluate_campaign(campaign) + self.evaluate_tenant(campaign)
