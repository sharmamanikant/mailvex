from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any, cast
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Campaign,
    CampaignRecipient,
    Contact,
    DeliveryJob,
    EmailAccount,
    Message,
    NormalizedDeliveryEvent,
)

# --------------------------------------------------------------------------- #
# Metric buckets (kept aligned with delivery_jobs / delivery_events helpers)
# --------------------------------------------------------------------------- #
# DeliveryJob send-path buckets.
JOB_SENT_STATUSES = {"SENT", "DELIVERED"}
JOB_BLOCKED_STATUSES = {"BLOCKED"}
JOB_FAILED_STATUSES = {"FAILED"}

# Message fallback buckets (legacy / pre-delivery-queue campaigns).
MESSAGE_SENT_STATUSES = {"SENT", "ACCEPTED", "DELIVERED"}
MESSAGE_QUEUED_STATUSES = {"QUEUED", "DEFERRED", "PROCESSING"}

# NormalizedDeliveryEvent outcome types that drive the dashboard.
EVENT_OUTCOME_TYPES = {
    "DELIVERED",
    "TEMPORARY_FAILURE",
    "HARD_BOUNCE",
    "COMPLAINT",
    "UNSUBSCRIBED",
}

# Throttling is recorded as a durable failure_code on the delivery job.
THROTTLE_FAILURE_CODE = "PROVIDER_THROTTLED"

# Sender account state buckets (see health.py / compliance.py).
UNAVAILABLE_SENDER_STATUSES = {
    "DISABLED",
    "DISCONNECTED",
    "SUSPENDED",
    "REAUTH_REQUIRED",
    "HEALTH_CRITICAL",
}
HEALTHY_SENDER_STATUSES = {"CONNECTED", "HEALTHY", "HEALTH_WARNING"}

# Campaign lifecycle buckets.
CAMPAIGN_ACTIVE_STATUSES = {"RUNNING", "PAUSED"}
CAMPAIGN_SCHEDULED_STATUSES = {"SCHEDULED", "APPROVED"}
CAMPAIGN_COMPLETED_STATUSES = {"COMPLETED"}

# Contact status buckets.
CONTACT_INVALID_STATUSES = {"INVALID"}
CONTACT_UNSUBSCRIBED_STATUSES = {"UNSUBSCRIBED"}


def _parse_datetime(value: str | None) -> datetime | None:
    """Parse an ISO date/datetime into an aware UTC datetime (mirrors
    campaign_analytics). A bare 'YYYY-MM-DD' is treated as midnight UTC."""
    if not value:
        return None
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def _window(
    range: str | None,
    start_date: str | None,
    end_date: str | None,
) -> tuple[datetime | None, datetime | None]:
    """Resolve the report time window. Returns (start, end) aware datetimes,
    either of which may be None to mean 'no bound'."""
    now = datetime.now(UTC)
    if range == "today":
        start = now.replace(hour=0, minute=0, second=0, microsecond=0)
        return start, now
    if range == "7d":
        return now - timedelta(days=7), now
    if range == "30d":
        return now - timedelta(days=30), now
    if range == "90d":
        return now - timedelta(days=90), now
    # custom (or undefined): use explicit bounds if provided
    return _parse_datetime(start_date), _parse_datetime(end_date)


class ReportingError(ValueError):
    pass


class ReportingService:
    """Business reporting, computed from tenant-scoped grouped SQL.

    All counts are produced with GROUP BY aggregations (a handful of queries per
    report) rather than per-row / per-campaign counting, so the dashboard does
    not scan individual message rows in N+1 loops. Outcomes come from the
    de-duplicated ``NormalizedDeliveryEvent`` table; the send-path funnel comes
    from the idempotent ``DeliveryJob`` queue (one row per recipient).
    """

    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    # ------------------------------------------------------------------ #
    # Windows
    # ------------------------------------------------------------------ #
    def _window(
        self, range: str | None, start_date: str | None, end_date: str | None
    ) -> tuple[datetime | None, datetime | None]:
        start, end = _window(range, start_date, end_date)
        if start is not None and end is not None and start > end:
            raise ReportingError("Start date must be before end date")
        return start, end

    def _range_clause(self, column: Any, start: datetime | None, end: datetime | None) -> list[Any]:
        clauses: list[Any] = []
        if start is not None:
            clauses.append(column >= start)
        if end is not None:
            clauses.append(column <= end)
        return clauses

    @staticmethod
    def _count_map(rows: Any) -> dict[str, int]:
        out: dict[str, int] = {}
        for key, count in rows:
            if key is not None:
                out[str(key)] = int(count)
        return out

    # ------------------------------------------------------------------ #
    # Contacts
    # ------------------------------------------------------------------ #
    def contact_report(
        self,
        range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, int]:
        start, end = self._window(range, start_date, end_date)
        base = [Contact.tenant_id == self.tenant_id]
        base.extend(self._range_clause(Contact.created_at, start, end))

        status_rows = self.session.execute(
            select(Contact.status, func.count(Contact.id))
            .where(*base)
            .group_by(Contact.status)
        ).all()
        by_status = self._count_map(status_rows)

        invalid_extra = int(
            self.session.scalar(
                select(func.count(Contact.id)).where(
                    *base,
                    Contact.validation_status == "INVALID",
                    Contact.status.notin_(CONTACT_INVALID_STATUSES),
                )
            )
            or 0
        )
        suppressed = int(
            self.session.scalar(
                select(func.count(Contact.id)).where(
                    *base,
                    Contact.suppression_status != "CLEAR",
                )
            )
            or 0
        )

        total = sum(by_status.values())
        return {
            "total": total,
            "active": by_status.get("ACTIVE", 0),
            "unsubscribed": by_status.get("UNSUBSCRIBED", 0),
            "suppressed": suppressed,
            "invalid": by_status.get("INVALID", 0) + invalid_extra,
            "inactive": by_status.get("INACTIVE", 0),
            "bounced": by_status.get("BOUNCED", 0),
        }

    # ------------------------------------------------------------------ #
    # Campaigns
    # ------------------------------------------------------------------ #
    def campaign_overview(
        self,
        range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, object]:
        start, end = self._window(range, start_date, end_date)
        clauses: list[Any] = [Campaign.tenant_id == self.tenant_id]
        clauses.extend(self._range_clause(Campaign.created_at, start, end))
        rows = self.session.execute(
            select(Campaign.status, func.count(Campaign.id))
            .where(*clauses)
            .group_by(Campaign.status)
        ).all()
        by_status = self._count_map(rows)

        active = sum(by_status.get(s, 0) for s in CAMPAIGN_ACTIVE_STATUSES)
        scheduled = sum(by_status.get(s, 0) for s in CAMPAIGN_SCHEDULED_STATUSES)
        completed = sum(by_status.get(s, 0) for s in CAMPAIGN_COMPLETED_STATUSES)
        return {
            "total": sum(by_status.values()),
            "active": active,
            "scheduled": scheduled,
            "completed": completed,
            "by_status": by_status,
        }

    # ------------------------------------------------------------------ #
    # Delivery (dashboard) + campaign/sender send-path helpers
    # ------------------------------------------------------------------ #
    def _delivery_job_counts(
        self,
        campaign_ids: list[UUID] | None = None,
        sender_ids: list[UUID] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, int]:
        """Sent / blocked / failed / throttled from the DeliveryJob queue."""
        where: list[Any] = [DeliveryJob.tenant_id == self.tenant_id]
        if campaign_ids is not None:
            where.append(DeliveryJob.campaign_id.in_(campaign_ids))
        if sender_ids is not None:
            where.append(DeliveryJob.sender_id.in_(sender_ids))
        where.extend(self._range_clause(DeliveryJob.completed_at, start, end))

        counts: dict[str, int] = {
            "jobs": 0,
            "sent": 0,
            "blocked": 0,
            "failed": 0,
            "throttled": 0,
        }
        rows = self.session.execute(
            select(DeliveryJob.status, func.count(DeliveryJob.id))
            .where(*where)
            .group_by(DeliveryJob.status)
        ).all()
        for status, count in rows:
            status_s = str(status)
            counts["jobs"] += int(count)
            if status_s in JOB_SENT_STATUSES:
                counts["sent"] += int(count)
            elif status_s in JOB_BLOCKED_STATUSES:
                counts["blocked"] += int(count)
            elif status_s in JOB_FAILED_STATUSES:
                counts["failed"] += int(count)
        if counts["jobs"]:
            throttle_clauses: list[Any] = [DeliveryJob.tenant_id == self.tenant_id,
                                            DeliveryJob.failure_code == THROTTLE_FAILURE_CODE]
            if campaign_ids is not None:
                throttle_clauses.append(DeliveryJob.campaign_id.in_(campaign_ids))
            if sender_ids is not None:
                throttle_clauses.append(DeliveryJob.sender_id.in_(sender_ids))
            throttle_clauses.extend(self._range_clause(DeliveryJob.completed_at, start, end))
            counts["throttled"] = int(
                self.session.scalar(select(func.count(DeliveryJob.id)).where(*throttle_clauses)) or 0
            )
        return counts

    def _message_fallback_counts(
        self,
        campaign_ids: list[UUID] | None = None,
        sender_ids: list[UUID] | None = None,
    ) -> dict[str, int]:
        """Legacy counts for tenants with no delivery_jobs rows."""
        counts: dict[str, int] = {"sent": 0, "queued": 0, "bounced": 0}
        where: list[Any] = [Message.tenant_id == self.tenant_id]
        if campaign_ids is not None:
            where.append(Message.campaign_id.in_(campaign_ids))
        if sender_ids is not None:
            where.append(Message.sender_id.in_(sender_ids))
        rows = self.session.execute(
            select(Message.status, func.count(Message.id))
            .where(*where)
            .group_by(Message.status)
        ).all()
        for status, count in rows:
            status_s = str(status)
            if status_s in MESSAGE_SENT_STATUSES:
                counts["sent"] += int(count)
            elif status_s in MESSAGE_QUEUED_STATUSES:
                counts["queued"] += int(count)
            elif status_s == "BOUNCED":
                counts["bounced"] += int(count)
        return counts

    def _event_outcome_counts(
        self,
        campaign_ids: list[UUID] | None = None,
        sender_ids: list[UUID] | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
    ) -> dict[str, int]:
        """Delivered / bounced / complaints / unsubscribed / temp fail from events."""
        where: list[Any] = [
            NormalizedDeliveryEvent.tenant_id == self.tenant_id,
            NormalizedDeliveryEvent.event_type.in_(tuple(EVENT_OUTCOME_TYPES)),
        ]
        if campaign_ids is not None:
            where.append(Message.campaign_id.in_(campaign_ids))
        if sender_ids is not None:
            where.append(Message.sender_id.in_(sender_ids))
        where.extend(self._range_clause(NormalizedDeliveryEvent.event_time, start, end))

        rows = self.session.execute(
            select(NormalizedDeliveryEvent.event_type, func.count(NormalizedDeliveryEvent.id))
            .join(Message, Message.id == NormalizedDeliveryEvent.message_id)
            .where(*where)
            .group_by(NormalizedDeliveryEvent.event_type)
        ).all()
        by_type = self._count_map(rows)
        return {
            "delivered": by_type.get("DELIVERED", 0),
            "bounced": by_type.get("HARD_BOUNCE", 0),
            "complaints": by_type.get("COMPLAINT", 0),
            "unsubscribed": by_type.get("UNSUBSCRIBED", 0),
            "temporary_failures": by_type.get("TEMPORARY_FAILURE", 0),
        }

    def delivery_overview(
        self,
        range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, int]:
        start, end = self._window(range, start_date, end_date)
        jobs = self._delivery_job_counts(start=start, end=end)
        events = self._event_outcome_counts(start=start, end=end)
        # Fall back to legacy Message counts when there is no delivery queue.
        if jobs["jobs"] == 0:
            legacy = self._message_fallback_counts()
            sent = legacy["sent"]
            bounced = legacy["bounced"]
        else:
            sent = jobs["sent"]
            bounced = events["bounced"]
        return {
            "sent": sent,
            "delivered": events["delivered"],
            "bounced": bounced,
            "unsubscribed": events["unsubscribed"],
            "complaints": events["complaints"],
            "temporary_failures": events["temporary_failures"],
            "blocked": jobs["blocked"],
            "failed": jobs["failed"],
        }

    # ------------------------------------------------------------------ #
    # Senders
    # ------------------------------------------------------------------ #
    def _sender_health_counts(self) -> dict[str, int]:
        where = [EmailAccount.tenant_id == self.tenant_id]
        status_rows = self.session.execute(
            select(EmailAccount.status, func.count(EmailAccount.id))
            .where(*where)
            .group_by(EmailAccount.status)
        ).all()
        by_status = self._count_map(status_rows)

        healthy_scores = int(
            self.session.scalar(
                select(func.count(EmailAccount.id)).where(
                    *where,
                    EmailAccount.health_score >= 80,
                    EmailAccount.status.notin_(tuple(UNAVAILABLE_SENDER_STATUSES)),
                )
            )
            or 0
        )
        unavailable = sum(
            by_status.get(s, 0) for s in UNAVAILABLE_SENDER_STATUSES
        )

        healthy = healthy_scores + by_status.get("HEALTHY", 0)
        connected = sum(by_status.values()) - unavailable
        return {
            "total": sum(by_status.values()),
            "connected": connected,
            "healthy": healthy,
            "needs_attention": unavailable,
        }

    def sender_overview(self) -> dict[str, int]:
        return self._sender_health_counts()

    def sender_report(
        self,
        range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, object]]:
        start, end = self._window(range, start_date, end_date)
        senders = self.session.scalars(
            select(EmailAccount)
            .where(EmailAccount.tenant_id == self.tenant_id)
            .order_by(EmailAccount.email)
        ).all()
        if not senders:
            return []
        sender_ids = [s.id for s in senders]

        per_sender_jobs = self.session.execute(
            select(DeliveryJob.sender_id, DeliveryJob.status, func.count(DeliveryJob.id))
            .where(
                DeliveryJob.tenant_id == self.tenant_id,
                DeliveryJob.sender_id.in_(sender_ids),
                *self._range_clause(DeliveryJob.completed_at, start, end),
            )
            .group_by(DeliveryJob.sender_id, DeliveryJob.status)
        ).all()
        job_map: dict[UUID, dict[str, int]] = {
            s.id: {"messages": 0, "successful": 0, "failed": 0, "blocked": 0} for s in senders
        }
        for sender_id, status, count in per_sender_jobs:
            status_s = str(status)
            job_map[sender_id]["messages"] += int(count)
            if status_s in JOB_SENT_STATUSES:
                job_map[sender_id]["successful"] += int(count)
            elif status_s in JOB_FAILED_STATUSES:
                job_map[sender_id]["failed"] += int(count)
            elif status_s in JOB_BLOCKED_STATUSES:
                job_map[sender_id]["blocked"] += int(count)

        throttle_map: dict[UUID, int] = {}
        throttle_rows = self.session.execute(
            select(DeliveryJob.sender_id, func.count(DeliveryJob.id))
            .where(
                DeliveryJob.tenant_id == self.tenant_id,
                DeliveryJob.sender_id.in_(sender_ids),
                DeliveryJob.failure_code == THROTTLE_FAILURE_CODE,
                *self._range_clause(DeliveryJob.completed_at, start, end),
            )
            .group_by(DeliveryJob.sender_id)
        ).all()
        for sender_id, count in throttle_rows:
            throttle_map[sender_id] = int(count)

        event_map: dict[UUID, dict[str, int]] = {}
        event_rows = self.session.execute(
            select(Message.sender_id, NormalizedDeliveryEvent.event_type, func.count(NormalizedDeliveryEvent.id))
            .join(Message, Message.id == NormalizedDeliveryEvent.message_id)
            .where(
                NormalizedDeliveryEvent.tenant_id == self.tenant_id,
                Message.sender_id.in_(sender_ids),
                NormalizedDeliveryEvent.event_type.in_(tuple(EVENT_OUTCOME_TYPES)),
                *self._range_clause(NormalizedDeliveryEvent.event_time, start, end),
            )
            .group_by(Message.sender_id, NormalizedDeliveryEvent.event_type)
        ).all()
        for sender_id, event_type, count in event_rows:
            ev = event_map.setdefault(sender_id, {})
            ev[str(event_type)] = int(count)

        rows: list[dict[str, object]] = []
        for sender in senders:
            j = job_map[sender.id]
            temp_failures = int(event_map.get(sender.id, {}).get("TEMPORARY_FAILURE", 0))
            successful = j["successful"]
            if j["messages"] == 0:
                legacy = self._message_fallback_counts(sender_ids=[sender.id])
                successful = legacy["sent"]
            rows.append(
                {
                    "sender_id": str(sender.id),
                    "sender_name": sender.display_name or sender.email,
                    "sender_email": sender.email,
                    "status": sender.status,
                    "health_score": float(sender.health_score) if sender.health_score is not None else None,
                    "health": self._sender_state(sender),
                    "messages": j["messages"],
                    "successful": successful,
                    "failed": j["failed"],
                    "temporary_failures": temp_failures,
                    "provider_throttling": throttle_map.get(sender.id, 0),
                    "blocked": j["blocked"],
                }
            )
        rows.sort(key=lambda r: cast(int, r["messages"]), reverse=True)
        return rows

    @staticmethod
    def _sender_state(sender: EmailAccount) -> str:
        if sender.status in UNAVAILABLE_SENDER_STATUSES:
            return "NEEDS_ATTENTION"
        if sender.health_score is not None and sender.health_score < 50:
            return "NEEDS_ATTENTION"
        return "HEALTHY"

    # ------------------------------------------------------------------ #
    # Campaign report
    # ------------------------------------------------------------------ #
    def campaign_report(
        self,
        range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> list[dict[str, object]]:
        start, end = self._window(range, start_date, end_date)
        campaigns = self.session.scalars(
            select(Campaign)
            .where(Campaign.tenant_id == self.tenant_id)
            .order_by(Campaign.created_at.desc())
        ).all()
        if not campaigns:
            return []
        campaign_ids = [c.id for c in campaigns]

        recipients_map: dict[UUID, int] = {}
        rec_rows = self.session.execute(
            select(CampaignRecipient.campaign_id, func.count(CampaignRecipient.id))
            .where(
                CampaignRecipient.tenant_id == self.tenant_id,
                CampaignRecipient.campaign_id.in_(campaign_ids),
            )
            .group_by(CampaignRecipient.campaign_id)
        ).all()
        for campaign_id, count in rec_rows:
            recipients_map[campaign_id] = int(count)

        jobs_map: dict[UUID, dict[str, int]] = {c.id: {"sent": 0, "blocked": 0, "failed": 0, "jobs": 0} for c in campaigns}
        job_rows = self.session.execute(
            select(DeliveryJob.campaign_id, DeliveryJob.status, func.count(DeliveryJob.id))
            .where(
                DeliveryJob.tenant_id == self.tenant_id,
                DeliveryJob.campaign_id.in_(campaign_ids),
                *self._range_clause(DeliveryJob.completed_at, start, end),
            )
            .group_by(DeliveryJob.campaign_id, DeliveryJob.status)
        ).all()
        for campaign_id, status, count in job_rows:
            status_s = str(status)
            jobs_map[campaign_id]["jobs"] += int(count)
            if status_s in JOB_SENT_STATUSES:
                jobs_map[campaign_id]["sent"] += int(count)
            elif status_s in JOB_BLOCKED_STATUSES:
                jobs_map[campaign_id]["blocked"] += int(count)
            elif status_s in JOB_FAILED_STATUSES:
                jobs_map[campaign_id]["failed"] += int(count)

        event_map: dict[UUID, dict[str, int]] = {c.id: {} for c in campaigns}
        event_rows = self.session.execute(
            select(Message.campaign_id, NormalizedDeliveryEvent.event_type, func.count(NormalizedDeliveryEvent.id))
            .join(Message, Message.id == NormalizedDeliveryEvent.message_id)
            .where(
                NormalizedDeliveryEvent.tenant_id == self.tenant_id,
                Message.campaign_id.in_(campaign_ids),
                NormalizedDeliveryEvent.event_type.in_(tuple(EVENT_OUTCOME_TYPES)),
                *self._range_clause(NormalizedDeliveryEvent.event_time, start, end),
            )
            .group_by(Message.campaign_id, NormalizedDeliveryEvent.event_type)
        ).all()
        for campaign_id, event_type, count in event_rows:
            event_map[campaign_id][str(event_type)] = int(count)

        rows: list[dict[str, object]] = []
        for campaign in campaigns:
            j = jobs_map[campaign.id]
            ev = event_map[campaign.id]
            sent = j["sent"]
            if j["jobs"] == 0:
                legacy = self._message_fallback_counts(campaign_ids=[campaign.id])
                sent = legacy["sent"] if sent == 0 else sent
            deliv = ev.get("DELIVERED", 0)
            total = recipients_map.get(campaign.id, 0)
            rows.append(
                {
                    "campaign_id": str(campaign.id),
                    "campaign_name": campaign.name,
                    "status": campaign.status,
                    "recipients": total,
                    "sent": sent,
                    "delivered": deliv,
                    "bounced": ev.get("HARD_BOUNCE", 0),
                    "blocked": j["blocked"],
                    "unsubscribed": ev.get("UNSUBSCRIBED", 0),
                    "complaints": ev.get("COMPLAINT", 0),
                    "failed": j["failed"],
                    "temporary_failures": ev.get("TEMPORARY_FAILURE", 0),
                    "delivery_rate": round((deliv / total) * 100, 2) if total else 0.0,
                }
            )
        return rows

    # ------------------------------------------------------------------ #
    # Dashboard
    # ------------------------------------------------------------------ #
    def dashboard(
        self, range: str | None = None, start_date: str | None = None, end_date: str | None = None
    ) -> dict[str, object]:
        return {
            "tenant_id": str(self.tenant_id),
            "window": {
                "range": range or "custom",
                "start": start_date,
                "end": end_date,
            },
            "contacts": self.contact_report(range, start_date, end_date),
            "campaigns": self.campaign_overview(range, start_date, end_date),
            "delivery": self.delivery_overview(range, start_date, end_date),
            "senders": self.sender_overview(),
        }

    # ------------------------------------------------------------------ #
    # CSV export
    # ------------------------------------------------------------------ #
    def export_csv(
        self,
        report: str,
        range: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> str:
        if report == "campaigns":
            rows = self.campaign_report(range, start_date, end_date)
            header = ["campaign_id", "campaign_name", "status", "recipients", "sent", "delivered", "bounced", "blocked", "unsubscribed", "complaints", "failed", "delivery_rate"]
        elif report == "senders":
            rows = self.sender_report(range, start_date, end_date)
            header = ["sender_id", "sender_name", "sender_email", "status", "health_score", "health", "messages", "successful", "failed", "temporary_failures", "provider_throttling", "blocked"]
        elif report == "contacts":
            row = self.contact_report(range, start_date, end_date)
            header = ["total", "active", "unsubscribed", "suppressed", "invalid", "inactive", "bounced"]
            return "\n".join([",".join(header), ",".join(_csv_field(str(row[h])) for h in header)])
        else:
            raise ReportingError("Unsupported report for CSV export")

        lines = [",".join(header)]
        lines.extend(
            ",".join(_csv_field(str(row.get(h, ""))) for h in header) for row in rows
        )
        return "\n".join(lines)


def _csv_field(value: str) -> str:
    if "," in value or '"' in value or "\n" in value:
        return '"' + value.replace('"', '""') + '"'
    return value
