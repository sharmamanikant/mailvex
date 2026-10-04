"""Phase 20 plan catalog.

Data-driven plan definitions. Pricing and capacity live ONLY here (one
source of truth); application code refers to plan *codes* and *metric keys*
by constant. Plans and limits can change without touching feature code, and
limits are never embedded in service/business logic.
"""

from __future__ import annotations

from dataclasses import dataclass, replace
from typing import Any

# --------------------------------------------------------------------------- #
# Billing metrics (plan limit keys)
# --------------------------------------------------------------------------- #
METRIC_CONTACTS = "contacts"
METRIC_AI_GENERATIONS = "ai_generations"
METRIC_CAMPAIGNS = "campaigns"
METRIC_MESSAGES = "messages"
METRIC_SENDERS = "connected_senders"
METRIC_STORAGE = "storage_mb"
METRIC_SEATS = "team_members"

METRIC_LABELS = {
    METRIC_CONTACTS: "Contacts",
    METRIC_AI_GENERATIONS: "AI generations",
    METRIC_CAMPAIGNS: "Campaigns",
    METRIC_MESSAGES: "Messages sent",
    METRIC_SENDERS: "Connected senders",
    METRIC_STORAGE: "Storage",
    METRIC_SEATS: "Team members",
}

# A metric that counts the tenant's current standing (rows/bytes) versus a
# metric that resets every billing period (events within the period).
ABSOLUTE_METRICS = frozenset(
    {METRIC_CONTACTS, METRIC_SENDERS, METRIC_STORAGE, METRIC_SEATS}
)

# --------------------------------------------------------------------------- #
# Usage event types (immutable log)
# --------------------------------------------------------------------------- #
EVENT_CONTACT_CREATED = "CONTACT_CREATED"
EVENT_AI_GENERATION = "AI_GENERATION"
EVENT_CAMPAIGN_CREATED = "CAMPAIGN_CREATED"
EVENT_MESSAGE_SENT = "MESSAGE_SENT"
EVENT_SENDER_CONNECTED = "SENDER_CONNECTED"
EVENT_STORAGE_USED = "STORAGE_USED"
EVENT_TEAM_MEMBER_ADDED = "TEAM_MEMBER_ADDED"

EVENT_TYPES = frozenset(
    {
        EVENT_CONTACT_CREATED,
        EVENT_AI_GENERATION,
        EVENT_CAMPAIGN_CREATED,
        EVENT_MESSAGE_SENT,
        EVENT_SENDER_CONNECTED,
        EVENT_STORAGE_USED,
        EVENT_TEAM_MEMBER_ADDED,
    }
)

EVENT_TO_METRIC = {
    EVENT_CONTACT_CREATED: METRIC_CONTACTS,
    EVENT_AI_GENERATION: METRIC_AI_GENERATIONS,
    EVENT_CAMPAIGN_CREATED: METRIC_CAMPAIGNS,
    EVENT_MESSAGE_SENT: METRIC_MESSAGES,
    EVENT_SENDER_CONNECTED: METRIC_SENDERS,
    EVENT_STORAGE_USED: METRIC_STORAGE,
    EVENT_TEAM_MEMBER_ADDED: METRIC_SEATS,
}

# --------------------------------------------------------------------------- #
# Plans
# --------------------------------------------------------------------------- #
DEFAULT_PLAN_CODE = "free"


@dataclass(frozen=True)
class PlanLimit:
    metric: str
    limit: int | None  # None = unlimited

    @property
    def scope(self) -> str:
        return "absolute" if self.metric in ABSOLUTE_METRICS else "period"


@dataclass(frozen=True)
class BillingPlan:
    code: str
    name: str
    price_usd_mo: int
    features: tuple[str, ...]
    limits: tuple[PlanLimit, ...]

    def limit_for(self, metric: str) -> PlanLimit | None:
        for item in self.limits:
            if item.metric == metric:
                return item
        return None


def _limits(*pairs: tuple[str, int | None]) -> tuple[PlanLimit, ...]:
    return tuple(PlanLimit(metric=metric, limit=limit) for metric, limit in pairs)


PLANS: dict[str, BillingPlan] = {
    "free": BillingPlan(
        code="free",
        name="Free",
        price_usd_mo=0,
        features=(
            "Basic analytics",
            "AI reply assistant",
            "Single workspace",
        ),
        limits=_limits(
            (METRIC_CONTACTS, 1000),
            (METRIC_AI_GENERATIONS, 200),
            (METRIC_CAMPAIGNS, 20),
            (METRIC_MESSAGES, 5000),
            (METRIC_SENDERS, 2),
            (METRIC_STORAGE, 5120),
            (METRIC_SEATS, 2),
        ),
    ),
    "starter": BillingPlan(
        code="starter",
        name="Starter",
        price_usd_mo=49,
        features=(
            "Everything in Free",
            "Message Studio",
            "Custom domains",
            "Email support",
        ),
        limits=_limits(
            (METRIC_CONTACTS, 10_000),
            (METRIC_AI_GENERATIONS, 2_000),
            (METRIC_CAMPAIGNS, 100),
            (METRIC_MESSAGES, 50_000),
            (METRIC_SENDERS, 10),
            (METRIC_STORAGE, 51_200),
            (METRIC_SEATS, 10),
        ),
    ),
    "business": BillingPlan(
        code="business",
        name="Business",
        price_usd_mo=199,
        features=(
            "Everything in Starter",
            "Sender health center",
            "Compliance center",
            "Priority support",
        ),
        limits=_limits(
            (METRIC_CONTACTS, 50_000),
            (METRIC_AI_GENERATIONS, 20_000),
            (METRIC_CAMPAIGNS, 500),
            (METRIC_MESSAGES, 500_000),
            (METRIC_SENDERS, 50),
            (METRIC_STORAGE, 512_000),
            (METRIC_SEATS, 50),
        ),
    ),
    "enterprise": BillingPlan(
        code="enterprise",
        name="Enterprise",
        price_usd_mo=0,  # custom contracts; quoted via sales
        features=(
            "Everything in Business",
            "Unlimited capacity",
            "SSO / SCIM",
            "Dedicated support",
        ),
        limits=_limits(
            (METRIC_CONTACTS, None),
            (METRIC_AI_GENERATIONS, None),
            (METRIC_CAMPAIGNS, None),
            (METRIC_MESSAGES, None),
            (METRIC_SENDERS, None),
            (METRIC_STORAGE, None),
            (METRIC_SEATS, None),
        ),
    ),
}

PLAN_CODES = tuple(PLANS.keys())


def plan(plan_code: str) -> BillingPlan:
    try:
        return PLANS[plan_code]
    except KeyError as exc:
        raise ValueError(f"Unknown plan '{plan_code}'") from exc


def effective_limits(
    plan_code: str, custom_limits: dict[str, Any] | None, seats: int | None
) -> dict[str, PlanLimit]:
    """Merge plan defaults with per-tenant capacity overrides."""
    base = plan(plan_code)
    merged: dict[str, PlanLimit] = {
        item.metric: PlanLimit(metric=item.metric, limit=item.limit)
        for item in base.limits
    }
    overrides = dict((custom_limits or {}) or {})
    seats = seats or overrides.get(METRIC_SEATS)
    if seats is not None:
        merged[METRIC_SEATS] = replace(
            merged[METRIC_SEATS], limit=int(seats)
        )
    for metric, value in overrides.items():
        if metric not in merged or metric == METRIC_SEATS:
            continue
        merged[metric] = replace(
            merged[metric], limit=None if value is None else int(value)
        )
    return merged


def validate_plan_code(plan_code: str) -> None:
    if plan_code not in PLANS:
        raise ValueError(f"Unknown plan '{plan_code}'")