from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from sqlalchemy import select, update
from sqlalchemy.orm import Session

from app.models import (
    Campaign,
    CampaignRecipient,
    Contact,
    EmailAccount,
    ScheduledMessage,
)

SCHEDULED_STATES = {"QUEUED", "PROCESSING", "SENT", "DEFERRED", "FAILED", "CANCELLED"}
TERMINAL_STATES = {"SENT", "FAILED", "CANCELLED"}


class SchedulerError(ValueError):
    pass


class SchedulingNotAllowedError(SchedulerError):
    pass


class ScheduledMessageNotFoundError(LookupError):
    pass


class SchedulerService:
    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    def schedule_campaign(self, campaign_id: UUID) -> list[ScheduledMessage]:
        campaign = self._campaign(campaign_id)
        if campaign.status != "APPROVED":
            raise SchedulingNotAllowedError("Only approved campaigns can be scheduled")
        if not campaign.schedule_config.get("start_at"):
            raise SchedulingNotAllowedError("A campaign start time is required before scheduling")
        rows = list(self.session.scalars(select(CampaignRecipient).where(CampaignRecipient.tenant_id == self.tenant_id, CampaignRecipient.campaign_id == campaign.id)).all())
        sender = self.session.scalar(select(EmailAccount).where(EmailAccount.tenant_id == self.tenant_id, EmailAccount.id == campaign.sender_id))
        if sender is None or sender.status in {"DISABLED", "HEALTH_CRITICAL"}:
            raise SchedulingNotAllowedError("Sender is not available for scheduling")
        if sender.status not in {"CONNECTED", "HEALTH_WARNING"}:
            raise SchedulingNotAllowedError("Sender health is not acceptable for scheduling")
        scheduled: list[ScheduledMessage] = []
        for index, row in enumerate(rows):
            contact = self.session.scalar(select(Contact).where(Contact.id == row.contact_id, Contact.tenant_id == self.tenant_id))
            if contact is None:
                continue
            due_at = self.next_business_time(self._start_time(campaign, index), self._timezone(campaign, contact))
            key = f"campaign:{campaign.id}:recipient:{row.id}"
            existing = self.session.scalar(select(ScheduledMessage).where(ScheduledMessage.tenant_id == self.tenant_id, ScheduledMessage.idempotency_key == key))
            if existing is not None:
                if existing.status == "CANCELLED" and existing.failure_reason in {"Campaign paused", "Campaign cancelled"}:
                    existing.status = "QUEUED"
                    existing.failure_reason = None
                    existing.deferred_until = None
                    existing.due_at = due_at
                scheduled.append(existing)
                continue
            scheduled.append(ScheduledMessage(tenant_id=self.tenant_id, campaign_id=campaign.id, campaign_recipient_id=row.id, due_at=due_at, deferred_until=None, status="QUEUED", priority=int(campaign.schedule_config.get("priority", 0)), max_attempts=int(campaign.schedule_config.get("max_attempts", 5)), idempotency_key=key))
        campaign.status = "SCHEDULED"
        self.session.add_all(scheduled)
        self.session.commit()
        return scheduled

    def pause_campaign(self, campaign_id: UUID) -> Campaign:
        campaign = self._campaign(campaign_id)
        if campaign.status not in {"SCHEDULED", "RUNNING"}:
            raise SchedulerError("Campaign cannot be paused from its current state")
        campaign.status = "PAUSED"
        self.session.execute(update(ScheduledMessage).where(ScheduledMessage.tenant_id == self.tenant_id, ScheduledMessage.campaign_id == campaign.id, ScheduledMessage.status.in_(["QUEUED", "DEFERRED"])).values(status="CANCELLED", failure_reason="Campaign paused"))
        self.session.commit()
        return campaign

    def resume_campaign(self, campaign_id: UUID) -> Campaign:
        campaign = self._campaign(campaign_id)
        if campaign.status != "PAUSED":
            raise SchedulerError("Only paused campaigns can resume")
        campaign.status = "SCHEDULED"
        self.session.execute(update(ScheduledMessage).where(ScheduledMessage.tenant_id == self.tenant_id, ScheduledMessage.campaign_id == campaign.id, ScheduledMessage.status == "CANCELLED", ScheduledMessage.failure_reason == "Campaign paused").values(status="QUEUED", failure_reason=None))
        self.session.commit()
        return campaign

    def cancel_campaign(self, campaign_id: UUID) -> Campaign:
        campaign = self._campaign(campaign_id)
        if campaign.status in {"COMPLETED", "CANCELLED"}:
            raise SchedulerError("Campaign is already terminal")
        campaign.status = "CANCELLED"
        self.session.execute(update(ScheduledMessage).where(ScheduledMessage.tenant_id == self.tenant_id, ScheduledMessage.campaign_id == campaign.id, ScheduledMessage.status.in_(["QUEUED", "DEFERRED", "PROCESSING"])).values(status="CANCELLED", failure_reason="Campaign cancelled"))
        self.session.commit()
        return campaign

    def claim(self, scheduled_id: UUID) -> ScheduledMessage:
        item = self._scheduled(scheduled_id)
        if item.status not in {"QUEUED", "DEFERRED"}:
            raise SchedulerError("Scheduled message is not claimable")
        campaign = self._campaign(item.campaign_id)
        if campaign.status in {"PAUSED", "CANCELLED"}:
            item.status = "CANCELLED"
            item.failure_reason = f"Campaign {campaign.status.lower()}"
            self.session.commit()
            raise SchedulerError("Campaign is not active")
        item.status = "PROCESSING"
        self.session.commit()
        return item

    def mark_sent(self, scheduled_id: UUID) -> ScheduledMessage:
        item = self._scheduled(scheduled_id)
        item.status = "SENT"
        item.processed_at = datetime.now(UTC)
        self.session.commit()
        return item

    def defer(self, scheduled_id: UUID, reason: str, retry_after: int | None = None) -> ScheduledMessage:
        item = self._scheduled(scheduled_id)
        item.attempt_count += 1
        if item.attempt_count >= item.max_attempts:
            return self.fail(scheduled_id, "Retry limit exceeded: " + reason)
        delay = retry_after if retry_after is not None else min(3600, 2 ** item.attempt_count * 60)
        item.status = "DEFERRED"
        item.deferred_until = datetime.now(UTC) + timedelta(seconds=delay)
        item.failure_reason = reason
        self.session.commit()
        return item

    def fail(self, scheduled_id: UUID, reason: str) -> ScheduledMessage:
        item = self._scheduled(scheduled_id)
        item.status = "FAILED"
        item.failure_reason = reason
        item.processed_at = datetime.now(UTC)
        self.session.commit()
        return item

    def _campaign(self, campaign_id: UUID) -> Campaign:
        campaign = self.session.scalar(select(Campaign).where(Campaign.id == campaign_id, Campaign.tenant_id == self.tenant_id))
        if campaign is None:
            raise ScheduledMessageNotFoundError("Campaign not found")
        return campaign

    def _scheduled(self, scheduled_id: UUID) -> ScheduledMessage:
        item = self.session.scalar(select(ScheduledMessage).where(ScheduledMessage.id == scheduled_id, ScheduledMessage.tenant_id == self.tenant_id))
        if item is None:
            raise ScheduledMessageNotFoundError("Scheduled message not found")
        return item

    @staticmethod
    def _start_time(campaign: Campaign, offset: int) -> datetime:
        configured = campaign.schedule_config.get("start_at")
        if configured:
            value = datetime.fromisoformat(str(configured).replace("Z", "+00:00"))
            if value.tzinfo is None:
                try:
                    value = value.replace(
                        tzinfo=ZoneInfo(str(campaign.schedule_config.get("timezone_policy") or "UTC"))
                    )
                except ZoneInfoNotFoundError:
                    value = value.replace(tzinfo=UTC)
            return value + timedelta(minutes=offset)
        return datetime.now(UTC) + timedelta(minutes=offset)

    @staticmethod
    def _timezone(campaign: Campaign, contact: Contact) -> str:
        return str(campaign.schedule_config.get("timezone_policy") or getattr(contact, "timezone", None) or "UTC")

    @staticmethod
    def next_business_time(value: datetime, timezone_name: str) -> datetime:
        if value.tzinfo is None:
            value = value.replace(tzinfo=UTC)
        return value.astimezone(UTC)
