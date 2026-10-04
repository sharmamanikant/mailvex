from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    AuditLog,
    Campaign,
    CampaignRecipient,
    Contact,
    DeliveryJob,
    Message,
    NormalizedDeliveryEvent,
)

TERMINAL_SEND_STATUSES = {"SENT", "DELIVERED", "FAILED", "BLOCKED", "CANCELLED"}
WORKING_SEND_STATUSES = {"PENDING", "PROCESSING"}

# Rollup buckets for the delivery-job send path.
SEND_STATUS_BUCKETS = {
    "queued": {"PENDING"},
    "processing": {"PROCESSING"},
    "sent": {"SENT", "DELIVERED"},
    "blocked": {"BLOCKED"},
    "failed": {"FAILED"},
    "cancelled": {"CANCELLED"},
}

# Recipient-level status labels exposed to the UI (no provider internals).
RECIPIENT_STATUS_LABELS = {
    "PENDING": "Pending",
    "PROCESSING": "Processing",
    "SENT": "Sent",
    "DELIVERED": "Delivered",
    "FAILED": "Failed",
    "BLOCKED": "Blocked",
    "CANCELLED": "Cancelled",
}


class CampaignAnalyticsError(ValueError):
    pass


class CampaignAnalyticsNotFoundError(LookupError):
    pass


class CampaignAnalyticsService:
    """Phase 16 campaign analytics derived from durable, idempotent sources.

    Delivery/outcome metrics (delivered, temporary failures, hard bounces,
    unsubscribes, complaints) are counted from ``NormalizedDeliveryEvent`` rows,
    which are de-duplicated at the database level keyed on
    (tenant_id, provider, provider_event_id). Send-path counts (queued, sent,
    blocked, failed) come from ``DeliveryJob`` (one idempotent row per recipient)
    and ``Message`` rows. No counters are manually incremented here, so duplicate
    or out-of-order events cannot inflate statistics.
    """

    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    # ------------------------------------------------------------------ #
    # Lookup
    # ------------------------------------------------------------------ #
    def _campaign(self, campaign_id: UUID) -> Campaign:
        campaign = self.session.scalar(
            select(Campaign).where(
                Campaign.id == campaign_id,
                Campaign.tenant_id == self.tenant_id,
            )
        )
        if campaign is None:
            raise CampaignAnalyticsNotFoundError("Campaign not found")
        return campaign

    def campaign(self, campaign_id: UUID) -> Campaign:
        return self._campaign(campaign_id)

    # ------------------------------------------------------------------ #
    # Metrics
    # ------------------------------------------------------------------ #
    def metrics(self, campaign_id: UUID) -> dict[str, int]:
        campaign = self._campaign(campaign_id)

        total_recipients = int(
            self.session.scalar(
                select(func.count(CampaignRecipient.id)).where(
                    CampaignRecipient.tenant_id == self.tenant_id,
                    CampaignRecipient.campaign_id == campaign.id,
                )
            )
            or 0
        )

        # Send-path counts from the durable per-recipient queue.
        job_counts: dict[str, int] = {bucket: 0 for bucket in SEND_STATUS_BUCKETS}
        job_rows = self.session.execute(
            select(DeliveryJob.status, func.count(DeliveryJob.id))
            .where(
                DeliveryJob.tenant_id == self.tenant_id,
                DeliveryJob.campaign_id == campaign.id,
            )
            .group_by(DeliveryJob.status)
        ).all()
        for status, count in job_rows:
            for bucket, statuses in SEND_STATUS_BUCKETS.items():
                if status in statuses:
                    job_counts[bucket] += int(count)
        queued = job_counts["queued"]
        processing = job_counts["processing"]
        sent = job_counts["sent"]
        blocked = job_counts["blocked"]
        failed = job_counts["failed"]

        total_jobs = queued + processing + sent + blocked + failed

        # A campaign that predates the delivery queue (or uses the legacy
        # scheduled-message path) has no delivery_jobs rows and only Message
        # rows. Fall back to them so the dashboard is never undercounted for
        # older campaigns.
        if total_jobs == 0:
            sent = int(
                self.session.scalar(
                    select(func.count(Message.id)).where(
                        Message.tenant_id == self.tenant_id,
                        Message.campaign_id == campaign.id,
                        Message.status.in_({"SENT", "ACCEPTED", "DELIVERED"}),
                    )
                )
                or 0
            )
            queued = int(
                self.session.scalar(
                    select(func.count(Message.id)).where(
                        Message.tenant_id == self.tenant_id,
                        Message.campaign_id == campaign.id,
                        Message.status.in_({"QUEUED", "DEFERRED", "PROCESSING"}),
                    )
                )
                or 0
            )

        # Event-derived outcomes (reliable provider data only).
        event_rows = self.session.execute(
            select(NormalizedDeliveryEvent.event_type, func.count(NormalizedDeliveryEvent.id))
            .join(Message, Message.id == NormalizedDeliveryEvent.message_id)
            .where(
                NormalizedDeliveryEvent.tenant_id == self.tenant_id,
                Message.campaign_id == campaign.id,
            )
            .group_by(NormalizedDeliveryEvent.event_type)
        ).all()
        event_counts = {event_type: int(count) for event_type, count in event_rows}

        delivered = event_counts.get("DELIVERED", 0)
        temporary_failures = event_counts.get("TEMPORARY_FAILURE", 0)
        hard_bounces = event_counts.get("HARD_BOUNCE", 0)
        unsubscribes = event_counts.get("UNSUBSCRIBED", 0)
        complaints = event_counts.get("COMPLAINT", 0)

        return {
            "recipients": total_recipients,
            "queued": queued,
            "processing": processing,
            "sent": sent,
            "delivered": delivered,
            "temporary_failures": temporary_failures,
            "hard_bounces": hard_bounces,
            "unsubscribes": unsubscribes,
            "complaints": complaints,
            "blocked": blocked,
            "failed": failed,
        }

    def summary(self, campaign_id: UUID) -> dict[str, object]:
        """Top metrics plus percentage deltas."""
        campaign = self._campaign(campaign_id)
        metrics = self.metrics(campaign_id)
        total = metrics["recipients"] or 0

        def pct(value: int) -> float:
            return round((value / total) * 100, 2) if total else 0.0

        return {
            "campaign_id": str(campaign_id),
            "campaign_name": campaign.name,
            "status": campaign.status,
            "metrics": metrics,
            "percentages": {
                "sent": pct(metrics["sent"]),
                "delivered": pct(metrics["delivered"]),
                "bounced": pct(metrics["hard_bounces"]),
                "unsubscribed": pct(metrics["unsubscribes"]),
                "complained": pct(metrics["complaints"]),
                "blocked": pct(metrics["blocked"]),
                "failed": pct(metrics["failed"]),
            },
        }

    # ------------------------------------------------------------------ #
    # Timeline
    # ------------------------------------------------------------------ #
    def timeline(self, campaign_id: UUID) -> list[dict[str, object]]:
        campaign = self._campaign(campaign_id)
        events: list[dict[str, object]] = []

        events.append(
            {
                "event": "Created",
                "timestamp": campaign.created_at.isoformat()
                if campaign.created_at is not None
                else None,
            }
        )
        if campaign.approved_at is not None:
            events.append(
                {"event": "Approved", "timestamp": campaign.approved_at.isoformat()}
            )

        audit_actions = {
            "CAMPAIGN_CREATED": "Created",
            "CAMPAIGN_APPROVED": "Approved",
            "CAMPAIGN_SCHEDULED": "Scheduled",
            "CAMPAIGN_RUNNING": "Started",
            "CAMPAIGN_PAUSED": "Paused",
            "CAMPAIGN_RESUMED": "Resumed",
            "CAMPAIGN_COMPLETED": "Completed",
            "CAMPAIGN_CANCELLED": "Cancelled",
        }
        audit_rows = self.session.execute(
            select(AuditLog.action, AuditLog.created_at)
            .where(
                AuditLog.tenant_id == self.tenant_id,
                AuditLog.resource_type == "campaign",
                AuditLog.resource_id == campaign.id,
                AuditLog.action.in_(tuple(audit_actions)),
            )
            .order_by(AuditLog.created_at.asc())
        ).all()
        for action, created_at in audit_rows:
            events.append(
                {
                    "event": audit_actions[action],
                    "timestamp": created_at.isoformat()
                    if created_at is not None
                    else None,
                }
            )

        # Delivery-derived markers when audit rows are absent.
        if not any(item["event"] in {"Scheduled", "Started"} for item in events):
            first_job = self.session.scalar(
                select(DeliveryJob.scheduled_at)
                .where(
                    DeliveryJob.tenant_id == self.tenant_id,
                    DeliveryJob.campaign_id == campaign.id,
                )
                .order_by(DeliveryJob.scheduled_at.asc())
            )
            if first_job is not None:
                events.append({"event": "Scheduled", "timestamp": first_job.isoformat()})

        terminal = self.session.scalar(
            select(func.count(DeliveryJob.id)).where(
                DeliveryJob.tenant_id == self.tenant_id,
                DeliveryJob.campaign_id == campaign.id,
                DeliveryJob.status.in_(TERMINAL_SEND_STATUSES),
            )
        ) or 0
        total_jobs = self.session.scalar(
            select(func.count(DeliveryJob.id)).where(
                DeliveryJob.tenant_id == self.tenant_id,
                DeliveryJob.campaign_id == campaign.id,
            )
        ) or 0
        if total_jobs > 0 and terminal == total_jobs and campaign.status == "COMPLETED":
            latest = self.session.scalar(
                select(DeliveryJob.completed_at)
                .where(
                    DeliveryJob.tenant_id == self.tenant_id,
                    DeliveryJob.campaign_id == campaign.id,
                )
                .order_by(DeliveryJob.completed_at.desc())
            )
            if latest is not None:
                events.append({"event": "Completed", "timestamp": latest.isoformat()})

        # Deduplicate by (event, timestamp) preserving order.
        seen: set[tuple[str, str | None]] = set()
        unique: list[dict[str, object]] = []
        for item in events:
            event = str(item["event"])
            timestamp = item["timestamp"]
            timestamp_str: str | None = (
                timestamp if isinstance(timestamp, str) else None
            )
            key = (event, timestamp_str)
            if key not in seen:
                seen.add(key)
                unique.append(item)
        return unique

    # ------------------------------------------------------------------ #
    # Recipient activity (paginated)
    # ------------------------------------------------------------------ #
    def recipient_activity(
        self,
        campaign_id: UUID,
        *,
        page: int = 1,
        page_size: int = 50,
        status_filter: str | None = None,
        start_date: str | None = None,
        end_date: str | None = None,
    ) -> dict[str, object]:
        campaign = self._campaign(campaign_id)
        if page < 1 or page_size < 1 or page_size > 200:
            raise CampaignAnalyticsError("Invalid pagination parameters")

        filters = [
            DeliveryJob.tenant_id == self.tenant_id,
            DeliveryJob.campaign_id == campaign.id,
        ]
        if status_filter is not None:
            if status_filter not in RECIPIENT_STATUS_LABELS:
                raise CampaignAnalyticsError(f"Unknown status filter: {status_filter}")
            filters.append(DeliveryJob.status == status_filter)
        if start_date is not None:
            filters.append(DeliveryJob.scheduled_at >= _parse_datetime(start_date))
        if end_date is not None:
            filters.append(DeliveryJob.scheduled_at <= _parse_datetime(end_date))

        total = int(
            self.session.scalar(
                select(func.count(DeliveryJob.id)).where(*filters)
            )
            or 0
        )
        offset = (page - 1) * page_size

        # Latest normalized outcome per recipient (one query; no N+1).
        latest_events: dict[UUID, tuple[str, str | None]] = {}
        event_rows = self.session.execute(
            select(
                Message.contact_id,
                NormalizedDeliveryEvent.event_type,
                NormalizedDeliveryEvent.event_time,
            )
            .join(Message, Message.id == NormalizedDeliveryEvent.message_id)
            .where(
                NormalizedDeliveryEvent.tenant_id == self.tenant_id,
                Message.campaign_id == campaign.id,
            )
            .order_by(
                NormalizedDeliveryEvent.event_time.asc(),
                NormalizedDeliveryEvent.created_at.asc(),
            )
        ).all()
        # 'asc' ordering then overwrite gives the *latest* event per recipient.
        for contact_id, event_type, event_time in event_rows:
            latest_events[contact_id] = (event_type, event_time)

        rows = self.session.execute(
            select(
                Contact.id,
                Contact.first_name,
                Contact.last_name,
                Contact.email,
                DeliveryJob.status,
                DeliveryJob.failure_code,
                DeliveryJob.last_error,
                DeliveryJob.completed_at,
            )
            .join(Contact, Contact.id == DeliveryJob.recipient_id)
            .where(*filters)
            .order_by(Contact.email.asc())
            .offset(offset)
            .limit(page_size)
        ).all()

        items: list[dict[str, object]] = []
        for row in rows:
            status = row.status
            reason = row.last_error
            event_time = row.completed_at
            latest = latest_events.get(row.id)
            if latest is not None:
                latest_type, latest_time = latest
                event_time = latest_time or event_time
                if latest_type in {
                    "DELIVERED",
                    "HARD_BOUNCE",
                    "UNSUBSCRIBED",
                    "COMPLAINT",
                    "TEMPORARY_FAILURE",
                }:
                    status = {
                        "DELIVERED": "DELIVERED",
                        "HARD_BOUNCE": "BLOCKED",
                        "UNSUBSCRIBED": "CANCELLED",
                        "COMPLAINT": "BLOCKED",
                        "TEMPORARY_FAILURE": "FAILED",
                    }[latest_type]
                    reason = reason or latest_type.replace("_", " ").title()
            items.append(
                {
                    "name": f"{row.first_name or ''} {row.last_name or ''}".strip()
                    or row.email,
                    "email": row.email,
                    "status": RECIPIENT_STATUS_LABELS.get(status, status),
                    "status_code": status,
                    "timestamp": event_time.isoformat() if event_time is not None else None,
                    "reason": reason,
                }
            )

        return {
            "campaign_id": str(campaign_id),
            "count": len(items),
            "total": total,
            "page": page,
            "page_size": page_size,
            "items": items,
        }

    # ------------------------------------------------------------------ #
    # CSV export
    # ------------------------------------------------------------------ #
    def export_csv(self, campaign_id: UUID) -> str:
        """Recipient, status, event, timestamp, reason — no provider internals."""
        self._campaign(campaign_id)

        # Export every recipient, not just the first page.
        lines = ["recipient,status,event,timestamp,reason"]
        page = 1
        while True:
            batch = self.recipient_activity(campaign_id, page=page, page_size=200)
            items = batch["items"]
            if isinstance(items, list):
                for item in items:
                    if not isinstance(item, dict):
                        continue
                    timestamp = str(item.get("timestamp") or "")
                    reason = str(item.get("reason") or "").replace(",", ";")
                    lines.append(
                        ",".join(
                            [
                                _csv_field(str(item.get("email", ""))),
                                _csv_field(str(item.get("status", ""))),
                                _csv_field(str(item.get("status_code", ""))),
                                _csv_field(timestamp),
                                _csv_field(reason),
                            ]
                        )
                    )
            total = batch.get("total")
            total_int = int(total) if isinstance(total, (int,)) else 0
            if isinstance(total, float) or isinstance(total, int) or isinstance(total, str):
                total_int = int(total)
            if page * 200 >= total_int:
                break
            page += 1
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    # Reports (durable event derived)
    # ------------------------------------------------------------------ #
    def campaign_report(self) -> list[dict[str, object]]:
        campaigns = self.session.scalars(
            select(Campaign)
            .where(Campaign.tenant_id == self.tenant_id)
            .order_by(Campaign.created_at.desc())
        ).all()
        return [self._campaign_summary_row(campaign) for campaign in campaigns]

    def _campaign_summary_row(self, campaign: Campaign) -> dict[str, object]:
        m = self.metrics(campaign.id)
        total = m["recipients"] or 0
        return {
            "campaign_id": str(campaign.id),
            "campaign_name": campaign.name,
            "status": campaign.status,
            "recipients": m["recipients"],
            "sent": m["sent"],
            "delivered": m["delivered"],
            "bounced": m["hard_bounces"],
            "unsubscribed": m["unsubscribes"],
            "complained": m["complaints"],
            "blocked": m["blocked"],
            "failed": m["failed"],
            "delivery_rate": round((m["delivered"] / total) * 100, 2) if total else 0.0,
        }

    def sender_report(self) -> list[dict[str, object]]:
        campaign_ids = list(
            self.session.scalars(
                select(Campaign.sender_id)
                .where(Campaign.tenant_id == self.tenant_id)
                .distinct()
            ).all()
        )
        rows: list[dict[str, object]] = []
        for sender_id in campaign_ids:
            if sender_id is None:
                continue
            campaigns = self.session.scalars(
                select(Campaign).where(
                    Campaign.tenant_id == self.tenant_id,
                    Campaign.sender_id == sender_id,
                )
            ).all()
            delivered = 0
            sent = 0
            complains = 0
            for campaign in campaigns:
                m = self.metrics(campaign.id)
                delivered += m["delivered"]
                sent += m["sent"]
                complains += m["complaints"]
            rows.append(
                {
                    "sender_id": str(sender_id),
                    "campaigns": len(campaigns),
                    "sent": sent,
                    "delivered": delivered,
                    "complaints": complains,
                    "delivery_rate": round((delivered / sent) * 100, 2) if sent else 0.0,
                }
            )
        rows.sort(key=_delivered_key, reverse=True)
        return rows


def _delivered_key(row: dict[str, object]) -> int:
    value = row.get("delivered", 0)
    try:
        return int(value if isinstance(value, (int, float, str)) else 0)
    except (TypeError, ValueError):
        return 0


def _csv_field(value: str) -> str:
    if "," in value or '"' in value or "\n" in value:
        return '"' + value.replace('"', '""') + '"'
    return value


def _parse_datetime(value: str) -> datetime:
    parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)