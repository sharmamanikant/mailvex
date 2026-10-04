"""Phase 20 SaaS usage + billing foundation.

Provider-agnostic metering (immutable usage events), data-driven plan
catalog, and limit enforcement. See ``plans.py`` (catalog) and ``usage.py``
(service).
"""

from app.billing.plans import (
    DEFAULT_PLAN_CODE,
    EVENT_AI_GENERATION,
    EVENT_CAMPAIGN_CREATED,
    EVENT_CONTACT_CREATED,
    EVENT_MESSAGE_SENT,
    EVENT_SENDER_CONNECTED,
    EVENT_STORAGE_USED,
    EVENT_TEAM_MEMBER_ADDED,
    EVENT_TO_METRIC,
    EVENT_TYPES,
    METRIC_LABELS,
    PLAN_CODES,
    PLANS,
    BillingPlan,
    PlanLimit,
)
from app.billing.usage import UsageLimitError, UsageService, month_start

__all__ = [
    "DEFAULT_PLAN_CODE",
    "EVENT_AI_GENERATION",
    "EVENT_CAMPAIGN_CREATED",
    "EVENT_CONTACT_CREATED",
    "EVENT_MESSAGE_SENT",
    "EVENT_SENDER_CONNECTED",
    "EVENT_STORAGE_USED",
    "EVENT_TEAM_MEMBER_ADDED",
    "EVENT_TO_METRIC",
    "EVENT_TYPES",
    "METRIC_LABELS",
    "PLANS",
    "PLAN_CODES",
    "BillingPlan",
    "PlanLimit",
    "UsageLimitError",
    "UsageService",
    "month_start",
]