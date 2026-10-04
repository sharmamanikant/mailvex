"""Phased 10Q campaign sender pools (System B).

A campaign can draw from a pool of ``SenderAccount`` senders instead of the
single legacy ``EmailAccount``. Rows live in ``CampaignSender`` (the STEP 2 join
table) and are validated against the sender's campaign opt-in, status, and
health before materialization. ``rotation`` returns the eligible senders in a
stable rotating order; the materializer assigns one per recipient (round-robin)
and records the choice on each ``DeliveryJob.sender_account_id``.
"""

from __future__ import annotations

from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, joinedload

from app.models import Campaign, CampaignSender, SenderAccount
from app.services.audit import AuditService
from app.services.integrations import (
    IntegrationConflictError,
    IntegrationNotFoundError,
    IntegrationValidationError,
)

_ELIGIBLE_HEALTH = {"HEALTHY", "DEGRADED"}
_BLOCKED_HEALTH = {"FAILED", "REAUTH_REQUIRED"}


class SenderPoolService:
    """Manages and selects the System B sender pool for a campaign."""

    def __init__(self, session: Session, tenant_id: UUID, actor_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id

    # ------------------------------------------------------------------ #
    # Pool management
    # ------------------------------------------------------------------ #
    def list_pool(self, campaign_id: UUID) -> list[CampaignSender]:
        return list(self.session.scalars(
            select(CampaignSender)
            .options(joinedload(CampaignSender.campaign))
            .where(
                CampaignSender.tenant_id == self.tenant_id,
                CampaignSender.campaign_id == campaign_id,
            )
            .order_by(CampaignSender.created_at)
        ).all())

    def add_sender(
        self,
        campaign_id: UUID,
        sender_id: UUID,
        *,
        daily_limit: int | None = None,
        enabled: bool = True,
    ) -> CampaignSender:
        campaign = self.session.scalar(
            select(Campaign).where(
                Campaign.id == campaign_id,
                Campaign.tenant_id == self.tenant_id,
            )
        )
        if campaign is None:
            raise IntegrationNotFoundError("Campaign not found")
        sender = self.session.scalar(
            select(SenderAccount).where(
                SenderAccount.id == sender_id,
                SenderAccount.tenant_id == self.tenant_id,
            )
        )
        if sender is None:
            raise IntegrationNotFoundError("Sender account not found")
        if sender.status != "ACTIVE":
            raise IntegrationValidationError("Sender account is not active")
        if not sender.campaign_enabled:
            raise IntegrationValidationError("Sender has not been enabled for campaign traffic")
        if sender.health_status in _BLOCKED_HEALTH:
            raise IntegrationValidationError(
                f"Sender is {sender.health_status}; fix its health before campaign use"
            )
        if daily_limit is not None and daily_limit < 1:
            raise IntegrationValidationError("daily_limit must be a positive integer")

        row = CampaignSender(
            tenant_id=self.tenant_id,
            campaign_id=campaign_id,
            sender_id=sender_id,
            daily_limit=daily_limit,
            enabled=enabled,
        )
        self.session.add(row)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            raise IntegrationConflictError("This sender is already in the campaign pool") from None
        AuditService(self.session, self.tenant_id, self.actor_id).record(
            "CAMPAIGN_SENDER_ADDED",
            "campaign",
            campaign_id,
            {"sender_id": str(sender_id), "email": sender.email, "provider": sender.provider},
        )
        self.session.commit()
        return row

    def remove_sender(self, campaign_id: UUID, sender_id: UUID) -> None:
        row = self.session.scalar(
            select(CampaignSender).where(
                CampaignSender.tenant_id == self.tenant_id,
                CampaignSender.campaign_id == campaign_id,
                CampaignSender.sender_id == sender_id,
            )
        )
        if row is None:
            raise IntegrationNotFoundError("Sender is not in the campaign pool")
        self.session.delete(row)
        AuditService(self.session, self.tenant_id, self.actor_id).record(
            "CAMPAIGN_SENDER_REMOVED",
            "campaign",
            campaign_id,
            {"sender_id": str(sender_id)},
        )
        self.session.commit()

    def set_enabled(self, campaign_id: UUID, sender_id: UUID, enabled: bool) -> CampaignSender:
        row = self.session.scalar(
            select(CampaignSender).where(
                CampaignSender.tenant_id == self.tenant_id,
                CampaignSender.campaign_id == campaign_id,
                CampaignSender.sender_id == sender_id,
            )
        )
        if row is None:
            raise IntegrationNotFoundError("Sender is not in the campaign pool")
        row.enabled = enabled
        AuditService(self.session, self.tenant_id, self.actor_id).record(
            "CAMPAIGN_SENDER_ENABLED" if enabled else "CAMPAIGN_SENDER_DISABLED",
            "campaign",
            campaign_id,
            {"sender_id": str(sender_id)},
        )
        self.session.commit()
        return row

    def set_daily_limit(
        self, campaign_id: UUID, sender_id: UUID, daily_limit: int | None
    ) -> CampaignSender:
        if daily_limit is not None and daily_limit < 1:
            raise IntegrationValidationError("daily_limit must be a positive integer")
        row = self.session.scalar(
            select(CampaignSender).where(
                CampaignSender.tenant_id == self.tenant_id,
                CampaignSender.campaign_id == campaign_id,
                CampaignSender.sender_id == sender_id,
            )
        )
        if row is None:
            raise IntegrationNotFoundError("Sender is not in the campaign pool")
        row.daily_limit = daily_limit
        AuditService(self.session, self.tenant_id, self.actor_id).record(
            "CAMPAIGN_SENDER_LIMIT_UPDATED",
            "campaign",
            campaign_id,
            {"sender_id": str(sender_id), "daily_limit": daily_limit},
        )
        self.session.commit()
        return row

    # ------------------------------------------------------------------ #
    # Selection
    # ------------------------------------------------------------------ #
    def rotation(self, campaign_id: UUID) -> list[SenderAccount]:
        """Eligible senders in stable rotation order (empty => no pool)."""
        rows = list(self.session.scalars(
            select(CampaignSender)
            .where(
                CampaignSender.tenant_id == self.tenant_id,
                CampaignSender.campaign_id == campaign_id,
                CampaignSender.enabled.is_(True),
            )
            .order_by(CampaignSender.created_at, CampaignSender.sender_id)
        ).all())
        eligible: list[SenderAccount] = []
        for row in rows:
            sender = self.session.scalar(
                select(SenderAccount).where(
                    SenderAccount.id == row.sender_id,
                    SenderAccount.tenant_id == self.tenant_id,
                )
            )
            if sender is None:
                continue
            if sender.status == "ACTIVE" and sender.campaign_enabled and sender.health_status in _ELIGIBLE_HEALTH:
                eligible.append(sender)
        return eligible