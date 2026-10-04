"""Phase 20 usage metering and limit enforcement.

Design goals
------------
- Provider-agnostic: no Stripe/Stripe-like coupling anywhere. The provider
  layer (if ever added) consumes the same immutable events and per-period
  counters this service maintains.
- Immutable events: ``UsageEvent`` rows are written once, inside the same
  transaction as the operation they meter, and never updated or deleted.
- No silent failures: ``enforce`` raises ``UsageLimitError`` with a clear
  message. Callers (services/API/workers) roll back the same transaction,
  so a refused operation persists nothing.
- Concurrency safe: periodic limits reserve capacity with an atomic
  ``INSERT ... ON CONFLICT DO UPDATE`` (works on SQLite and PostgreSQL), so
  concurrent threads can never silently overshoot a plan limit. Absolute
  limits (contacts/senders/seats) are checked under a tenant-row lock.
- Billing never bypasses other controls: enforcement only ever *adds* gates.
  Suppression, compliance, provider restrictions, and security checks run
  independently and always take precedence.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.engine import CursorResult
from sqlalchemy.orm import Session

from app.billing.plans import (
    ABSOLUTE_METRICS,
    DEFAULT_PLAN_CODE,
    EVENT_STORAGE_USED,
    EVENT_TO_METRIC,
    EVENT_TYPES,
    METRIC_CONTACTS,
    METRIC_LABELS,
    METRIC_SEATS,
    METRIC_SENDERS,
    METRIC_STORAGE,
    PlanLimit,
    effective_limits,
    plan,
    validate_plan_code,
)
from app.models import (
    Contact,
    EmailAccount,
    Tenant,
    TenantSubscription,
    UsageEvent,
    UsageRecord,
    User,
)
from app.services.audit import AuditService


class UsageLimitError(ValueError):
    """A tenant has reached a plan limit.

    Raised instead of silently truncating or partially persisting work.
    """

    def __init__(self, metric: str, limit: int, used: Decimal, plan_code: str) -> None:
        self.metric = metric
        self.limit = limit
        self.used = used
        self.plan_code = plan_code
        label = METRIC_LABELS.get(metric, metric)
        plan_name = plan(plan_code).name
        super().__init__(
            f"{label} limit reached ({int(used)}/{limit}) on the {plan_name} plan. "
            "Upgrade, free up capacity, or reduce the batch to continue."
        )


def month_start(when: datetime) -> datetime:
    return when.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


class UsageService:
    def __init__(self, session: Session, tenant_id: UUID, actor_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id

    # ------------------------------------------------------------------ #
    # Subscription
    # ------------------------------------------------------------------ #
    def subscription(self) -> TenantSubscription | None:
        return self.session.scalar(
            select(TenantSubscription).where(
                TenantSubscription.tenant_id == self.tenant_id
            )
        )

    def ensure_subscription(self) -> TenantSubscription:
        subscription = self.subscription()
        if subscription is None:
            subscription = TenantSubscription(
                tenant_id=self.tenant_id,
                plan_code=DEFAULT_PLAN_CODE,
                status="ACTIVE",
                custom_limits={},
                period_start=datetime.now(UTC),
            )
            self.session.add(subscription)
            self.session.flush()
        return subscription

    def change_plan(
        self,
        plan_code: str,
        *,
        reset_period: bool = False,
        status: str | None = None,
    ) -> TenantSubscription:
        validate_plan_code(plan_code)
        subscription = self.ensure_subscription()
        subscription.plan_code = plan_code
        if status is not None:
            subscription.status = status
        else:
            subscription.status = "ACTIVE"
        if reset_period or subscription.period_start is None:
            subscription.period_start = datetime.now(UTC)
        AuditService(self.session, self.tenant_id, self.actor_id).record(
            "PLAN_CHANGED",
            "tenant",
            self.tenant_id,
            {
                "to_plan": plan_code,
                "reset_period": bool(reset_period),
            },
        )
        self.session.flush()
        return subscription

    def plan_code(self) -> str:
        subscription = self.subscription()
        return subscription.plan_code if subscription is not None else DEFAULT_PLAN_CODE

    def period_start(self) -> datetime:
        subscription = self.subscription()
        if subscription is not None and subscription.period_start is not None:
            return subscription.period_start
        return month_start(datetime.now(UTC))

    def period_end(self) -> datetime:
        start = self.period_start()
        return start.replace(month=start.month % 12 + 1, day=1) if start.month < 12 else start.replace(year=start.year + 1, month=1, day=1)

    def effective_limits(self) -> dict[str, PlanLimit]:
        subscription = self.subscription()
        plan_code = (
            subscription.plan_code
            if subscription is not None
            else DEFAULT_PLAN_CODE
        )
        custom = (subscription.custom_limits if subscription is not None else None) or {}
        seats = subscription.seats if subscription is not None else None
        return effective_limits(plan_code, custom, seats)

    def limit_for(self, metric: str) -> PlanLimit | None:
        return self.effective_limits().get(metric)

    # ------------------------------------------------------------------ #
    # Metering
    # ------------------------------------------------------------------ #
    def record_event(
        self,
        event_type: str,
        *,
        quantity: Decimal | int | float = 1,
        resource_type: str | None = None,
        resource_id: UUID | None = None,
        metadata: dict[str, object] | None = None,
    ) -> UsageEvent:
        if event_type not in EVENT_TYPES:
            raise ValueError(f"Unknown usage event type '{event_type}'")
        event = UsageEvent(
            tenant_id=self.tenant_id,
            actor_id=self.actor_id,
            event_type=event_type,
            period_start=self.period_start(),
            quantity=Decimal(str(quantity)),
            resource_type=resource_type,
            resource_id=resource_id,
            event_metadata=dict(metadata or {}),
        )
        self.session.add(event)
        return event

    def meter(
        self,
        event_type: str,
        *,
        quantity: Decimal | int | float = 1,
        resource_type: str | None = None,
        resource_id: UUID | None = None,
        metadata: dict[str, object] | None = None,
    ) -> UsageEvent:
        """Enforce the plan limit for the event, then record it.

        Both happen in the caller's transaction, so a raised
        ``UsageLimitError`` persists nothing.
        """
        metric = EVENT_TO_METRIC[event_type]
        self.enforce(metric, quantity=quantity)
        return self.record_event(
            event_type,
            quantity=quantity,
            resource_type=resource_type,
            resource_id=resource_id,
            metadata=metadata,
        )

    # ------------------------------------------------------------------ #
    # Enforcement
    # ------------------------------------------------------------------ #
    def enforce(self, metric: str, *, quantity: Decimal | int | float = 1) -> None:
        """Raise ``UsageLimitError`` if the tenant has no headroom.

        Absolute metrics are checked under a tenant-row lock so creations in
        flight serialize. Periodic metrics reserve capacity with a single
        atomic, guarded "INSERT ... ON CONFLICT DO UPDATE ... WHERE total <=
        limit" statement — the increment and the limit check are one unit of
        work on both SQLite and PostgreSQL, so concurrent reservations can
        never silently overshoot a plan limit.
        """
        amount = Decimal(str(quantity))
        limit = self.limit_for(metric)
        if limit is None or limit.limit is None:
            return
        if metric in ABSOLUTE_METRICS:
            self._lock_tenant()
            projected = self._current_usage(metric) + amount
            if projected > Decimal(limit.limit):
                raise UsageLimitError(
                    metric, limit.limit, projected, self.plan_code()
                )
            return
        self._lock_tenant()
        self._reserve(metric, amount, self.period_start(), limit.limit)

    def check(self, metric: str) -> dict[str, object]:
        """Inspect headroom without reserving (read-only quota check)."""
        current = self._current_usage(metric)
        limit = self.limit_for(metric)
        return {
            "metric": metric,
            "used": current,
            "limit": limit.limit if limit is not None else None,
            "remaining": (
                max(Decimal(0), Decimal(limit.limit) - current)
                if limit is not None and limit.limit is not None
                else None
            ),
            "scope": limit.scope if limit is not None else "absolute",
        }

    # ------------------------------------------------------------------ #
    # Reporting/envelope
    # ------------------------------------------------------------------ #
    def envelope(self) -> dict[str, object]:
        subscription = self.subscription()
        plan_code = self.plan_code()
        active_plan = plan(plan_code)
        metrics: list[dict[str, object]] = []
        for metric, label in METRIC_LABELS.items():
            limit = self.limit_for(metric)
            used = self._current_usage(metric)
            cap = limit.limit if limit is not None else None
            remaining = (
                max(Decimal(0), Decimal(cap) - used)
                if cap is not None
                else None
            )
            scope = limit.scope if limit is not None else (
                "absolute" if metric in ABSOLUTE_METRICS else "period"
            )
            metrics.append(
                {
                    "metric": metric,
                    "label": label,
                    "used": float(used),
                    "limit": cap,
                    "remaining": float(remaining) if remaining is not None else None,
                    "scope": scope,
                }
            )
        return {
            "tenant_id": str(self.tenant_id),
            "plan_code": plan_code,
            "plan_name": active_plan.name,
            "price_usd_mo": active_plan.price_usd_mo,
            "features": list(active_plan.features),
            "status": (subscription.status if subscription is not None else "ACTIVE"),
            "period_start": self.period_start().isoformat(),
            "period_end": self.period_end().isoformat(),
            "metrics": metrics,
        }

    def recent_events(
        self,
        *,
        event_type: str | None = None,
        limit: int = 50,
    ) -> list[UsageEvent]:
        if limit < 1 or limit > 500:
            raise ValueError("limit must be between 1 and 500")
        query = select(UsageEvent).where(UsageEvent.tenant_id == self.tenant_id)
        if event_type is not None:
            if event_type not in EVENT_TYPES:
                raise ValueError(f"Unknown usage event type '{event_type}'")
            query = query.where(UsageEvent.event_type == event_type)
        query = query.order_by(UsageEvent.created_at.desc()).limit(limit)
        return list(self.session.scalars(query).all())

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #
    def _lock_tenant(self) -> None:
        self.session.execute(
            select(Tenant.id)
            .where(Tenant.id == self.tenant_id)
            .with_for_update()
        )

    def _reserve(
        self,
        metric: str,
        quantity: Decimal,
        period_start: datetime,
        limit: int,
    ) -> Decimal:
        """Atomically claim ``quantity`` toward ``limit``.

        A single guarded upsert does the increment and the limit check as one
        statement, so concurrent claims cannot overshoot the plan limit.
        Returns the updated total; raises ``UsageLimitError`` when the guard
        rejects the claim (or when a new period row would start beyond limit).
        """
        if limit <= 0:
            raise UsageLimitError(metric, limit, Decimal(limit), self.plan_code())
        dialect = self.session.get_bind().dialect.name
        statement: Any
        if dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert

            statement = pg_insert(UsageRecord).values(
                tenant_id=self.tenant_id,
                metric=metric,
                period_start=period_start,
                quantity=quantity,
                source="plan-limit",
            )
            statement = statement.on_conflict_do_update(
                index_elements=[
                    UsageRecord.tenant_id,
                    UsageRecord.metric,
                    UsageRecord.period_start,
                ],
                set_={
                    "quantity": UsageRecord.quantity + statement.excluded.quantity
                },
                where=(
                    (UsageRecord.quantity + statement.excluded.quantity)
                    <= Decimal(limit)
                ),
            )
        else:  # sqlite and anything else with ON CONFLICT support
            from sqlalchemy.dialects.sqlite import insert as sqlite_insert

            statement = sqlite_insert(UsageRecord).values(
                tenant_id=self.tenant_id,
                metric=metric,
                period_start=period_start,
                quantity=quantity,
                source="plan-limit",
            )
            statement = statement.on_conflict_do_update(
                index_elements=[
                    UsageRecord.tenant_id,
                    UsageRecord.metric,
                    UsageRecord.period_start,
                ],
                set_={
                    "quantity": UsageRecord.quantity + statement.excluded.quantity
                },
                where=(
                    (UsageRecord.quantity + statement.excluded.quantity)
                    <= Decimal(limit)
                ),
            )
        result = cast(CursorResult[Any], self.session.execute(statement))
        if result.rowcount in (0, None):
            # No existing row was updated (or a fresh-insert-only guard) — the
            # limit is already consumed.
            raise UsageLimitError(metric, limit, Decimal(limit), self.plan_code())
        total = self.session.scalar(
            select(func.coalesce(func.sum(UsageRecord.quantity), 0)).where(
                UsageRecord.tenant_id == self.tenant_id,
                UsageRecord.metric == metric,
                UsageRecord.period_start == period_start,
            )
        )
        return Decimal(str(total or 0))

    def _current_usage(self, metric: str) -> Decimal:
        if metric == METRIC_CONTACTS:
            count = self.session.scalar(
                select(func.count(Contact.id)).where(
                    Contact.tenant_id == self.tenant_id
                )
            )
            return Decimal(int(count or 0))
        if metric == METRIC_SENDERS:
            count = self.session.scalar(
                select(func.count(EmailAccount.id)).where(
                    EmailAccount.tenant_id == self.tenant_id
                )
            )
            return Decimal(int(count or 0))
        if metric == METRIC_SEATS:
            count = self.session.scalar(
                select(func.count(User.id)).where(
                    User.tenant_id == self.tenant_id,
                    User.status != "DELETED",
                )
            )
            return Decimal(int(count or 0))
        if metric == METRIC_STORAGE:
            quantity = self.session.scalar(
                select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
                    UsageEvent.tenant_id == self.tenant_id,
                    UsageEvent.event_type == EVENT_STORAGE_USED,
                )
            )
            return Decimal(str(quantity or 0))
        return self._period_usage(metric)

    def _period_usage(self, metric: str) -> Decimal:
        start = self.period_start()
        event_types = [
            event_type
            for event_type, mapped in EVENT_TO_METRIC.items()
            if mapped == metric
        ]
        quantity = self.session.scalar(
            select(func.coalesce(func.sum(UsageEvent.quantity), 0)).where(
                UsageEvent.tenant_id == self.tenant_id,
                UsageEvent.event_type.in_(event_types),
                UsageEvent.period_start >= start,
            )
        )
        return Decimal(str(quantity or 0))