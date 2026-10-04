from __future__ import annotations

from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    Message,
    MessageEvent,
    NormalizedDeliveryEvent,
    SenderAccount,
)
from app.services.audit import AuditService
from app.services.suppression_engine import SuppressionEngine
from app.utils.emails import normalize_email

# Canonical event types produced by normalization.
NORMALIZED_EVENT_TYPES = frozenset(
    {"DELIVERED", "TEMPORARY_FAILURE", "HARD_BOUNCE", "COMPLAINT", "UNSUBSCRIBED", "UNKNOWN"}
)

# Generic provider event labels -> canonical normalized type.
_PROVIDER_CANONICAL = {
    "GOOGLE": {
        "delivered": "DELIVERED",
        "bounce": "HARD_BOUNCE",
        "hard_bounce": "HARD_BOUNCE",
        "complaint": "COMPLAINT",
        "unsubscribe": "UNSUBSCRIBED",
        "temporary_failure": "TEMPORARY_FAILURE",
        "deferred": "TEMPORARY_FAILURE",
    },
    "MICROSOFT": {
        "delivered": "DELIVERED",
        "bounce": "HARD_BOUNCE",
        "hardBounce": "HARD_BOUNCE",
        "complaint": "COMPLAINT",
        "unsubscribe": "UNSUBSCRIBED",
        "temporaryFailure": "TEMPORARY_FAILURE",
        "deferred": "TEMPORARY_FAILURE",
    },
    "SMTP": {
        "delivered": "DELIVERED",
        "bounce": "HARD_BOUNCE",
        "hard_bounce": "HARD_BOUNCE",
        "complaint": "COMPLAINT",
        "unsubscribe": "UNSUBSCRIBED",
        "temporary_failure": "TEMPORARY_FAILURE",
        "deferred": "TEMPORARY_FAILURE",
        "soft_bounce": "TEMPORARY_FAILURE",
    },
}


class DeliveryEventError(ValueError):
    pass


def canonicalize_event_type(provider: str, provider_event: str) -> str:
    """Map a provider-specific event label to a canonical normalized type."""
    mapping = _PROVIDER_CANONICAL.get(provider.upper(), {})
    key = str(provider_event).strip().replace(" ", "_").lower()
    return mapping.get(key, "UNKNOWN")


class DeliveryEventService:
    """Consume verified provider delivery events idempotently and apply
    suppression/safety side effects.

    Webhook payloads have ALREADY passed signature verification before reaching
    this service; this layer only normalizes, de-duplicates, and applies effects.
    """

    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.suppression = SuppressionEngine(session, tenant_id)

    # ------------------------------------------------------------------
    # Idempotent persistence of the normalized event
    # ------------------------------------------------------------------
    def ensure_event(
        self,
        provider: str,
        provider_event_id: str,
        event_type: str,
        recipient: str,
        message_id: UUID | None = None,
        event_time: datetime | None = None,
        raw_reference: str | None = None,
    ) -> NormalizedDeliveryEvent:
        """Persist a normalized event exactly once (idempotent by provider id)."""
        normalized = normalize_email(recipient)
        record = NormalizedDeliveryEvent(
            tenant_id=self.tenant_id,
            provider=provider.upper(),
            provider_event_id=provider_event_id,
            event_type=event_type,
            recipient=normalized,
            message_id=message_id,
            event_time=event_time,
            raw_reference=raw_reference,
        )
        self.session.add(record)
        try:
            self.session.flush()
        except IntegrityError:
            self.session.rollback()
            existing = self.session.scalar(
                select(NormalizedDeliveryEvent).where(
                    NormalizedDeliveryEvent.tenant_id == self.tenant_id,
                    NormalizedDeliveryEvent.provider == provider.upper(),
                    NormalizedDeliveryEvent.provider_event_id == provider_event_id,
                )
            )
            if existing is None:
                raise
            return existing
        return record

    def process_event(
        self,
        provider: str,
        provider_event_id: str,
        provider_event: str,
        recipient: str,
        *,
        message_id: UUID | None = None,
        event_time: datetime | None = None,
        raw_reference: str | None = None,
        campaign_id: UUID | None = None,
        repeated_failure_threshold: int = 3,
    ) -> NormalizedDeliveryEvent:
        """Normalize, persist idempotently, and apply safety side effects.

        Returns the normalized event. If the event id was already processed it
        is returned without re-applying any side effect (no duplicate
        suppression, analytics, statistics, or audit).
        """
        event_type = canonicalize_event_type(provider, provider_event)
        if event_type not in NORMALIZED_EVENT_TYPES:
            event_type = "UNKNOWN"

        # De-duplicate BEFORE any side effect: a repeated webhook must not
        # double-apply suppression/audit/statistics.
        prior = self.session.scalar(
            select(NormalizedDeliveryEvent).where(
                NormalizedDeliveryEvent.tenant_id == self.tenant_id,
                NormalizedDeliveryEvent.provider == provider.upper(),
                NormalizedDeliveryEvent.provider_event_id == provider_event_id,
            )
        )
        if prior is not None:
            return prior

        record = self.ensure_event(
            provider,
            provider_event_id,
            event_type,
            recipient,
            message_id=message_id,
            event_time=event_time,
            raw_reference=raw_reference,
        )
        record.processed_at = datetime.now(UTC)

        normal_email = normalize_email(recipient)
        message = None
        if message_id is not None:
            message = self.session.scalar(
                select(Message).where(
                    Message.id == message_id,
                    Message.tenant_id == self.tenant_id,
                )
            )

        if event_type == "DELIVERED":
            if message is not None:
                message.status = "DELIVERED"
                self._append_message_event(message, "DELIVERED", provider_event_id, {"provider": provider.upper()}, event_time)
            self._bump_campaign_stat(message, "delivered")

        elif event_type == "HARD_BOUNCE":
            self.suppression.suppress(
                normal_email,
                "HARD_BOUNCE",
                source="provider-event",
                reason=raw_reference or f"Hard bounce reported by {provider.upper()}",
                provider=provider.upper(),
                campaign_id=campaign_id,
                contact_id=message.contact_id if message is not None else None,
            )
            if message is not None:
                message.status = "BOUNCED"
                self._append_message_event(message, "HARD_BOUNCE", provider_event_id, {"provider": provider.upper(), "diagnostic": raw_reference}, event_time)
            self._bump_campaign_stat(message, "hard_bounced")
            self._evaluate_sender_safety(message)

        elif event_type == "COMPLAINT":
            self.suppression.suppress(
                normal_email,
                "COMPLAINT",
                source="provider-event",
                reason=raw_reference or f"Complaint reported by {provider.upper()}",
                provider=provider.upper(),
                campaign_id=campaign_id,
                contact_id=message.contact_id if message is not None else None,
            )
            if message is not None:
                message.status = "COMPLAINED"
                self._append_message_event(message, "COMPLAINT", provider_event_id, {"provider": provider.upper()}, event_time)
            self._bump_campaign_stat(message, "complained")
            self._evaluate_sender_safety(message)

        elif event_type == "UNSUBSCRIBED":
            self.suppression.suppress(
                normal_email,
                "UNSUBSCRIBED",
                source="provider-event",
                reason=raw_reference or "Unsubscribed via provider",
                provider=provider.upper(),
                campaign_id=campaign_id,
                contact_id=message.contact_id if message is not None else None,
            )
            if message is not None:
                self._append_message_event(message, "UNSUBSCRIBED", provider_event_id, {"provider": provider.upper()}, event_time)
            self._bump_campaign_stat(message, "unsubscribed")

        elif event_type == "TEMPORARY_FAILURE":
            # Track recurring temporary failures; suppress only when the tenant
            # policy threshold is reached. Never a permanent one-off suppress.
            if message is not None:
                message.status = "DEFERRED"
                self._append_message_event(message, "TEMPORARY_FAILURE", provider_event_id, {"provider": provider.upper()}, event_time)
                count = self._temporary_failures(message.id)
                if count >= repeated_failure_threshold:
                    self.suppression.suppress(
                        normal_email,
                        "MANUAL",
                        source="repeated-temporary-failure",
                        reason="Repeated temporary delivery failures",
                        provider=provider.upper(),
                        campaign_id=campaign_id,
                        contact_id=message.contact_id,
                    )
            self._bump_campaign_stat(message, "temporary_failures")

        else:  # UNKNOWN
            if message is not None:
                self._append_message_event(message, "UNKNOWN", provider_event_id, {"provider": provider.upper()}, event_time)

        if message is not None and campaign_id is None:
            campaign_id = message.campaign_id

        AuditService(self.session, self.tenant_id).record(
            "DELIVERY_EVENT_PROCESSED",
            "delivery_event",
            record.id,
            {
                "provider": provider.upper(),
                "event_type": event_type,
                "provider_event_id": provider_event_id,
                "recipient": normal_email,
                "message_id": str(message_id) if message_id is not None else None,
            },
        )

        try:
            self.session.commit()
        except IntegrityError:
            # Concurrent duplicate: another worker won the race. The winning
            # record must exist now; re-query to return the persisted row.
            self.session.rollback()
            existing = self.session.scalar(
                select(NormalizedDeliveryEvent).where(
                    NormalizedDeliveryEvent.tenant_id == self.tenant_id,
                    NormalizedDeliveryEvent.provider == provider.upper(),
                    NormalizedDeliveryEvent.provider_event_id == provider_event_id,
                )
            )
            if existing is None:
                raise DeliveryEventError(
                    f"Could not resolve delivery event {provider_event_id} after race"
                ) from None
            return existing
        return record

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------
    def _temporary_failures(self, message_id: UUID) -> int:
        return len(
            list(
                self.session.scalars(
                    select(NormalizedDeliveryEvent).where(
                        NormalizedDeliveryEvent.tenant_id == self.tenant_id,
                        NormalizedDeliveryEvent.message_id == message_id,
                        NormalizedDeliveryEvent.event_type == "TEMPORARY_FAILURE",
                    )
                ).all()
            )
        )

    def _evaluate_sender_safety(self, message: Message | None) -> None:
        """Guardrail 12: re-evaluate and protective-pause the sender after a
        hard bounce or complaint. Only the System B sender pool accounts carry
        a compliance state; legacy senders are gated by sender health in the
        send-time compliance check instead.
        """
        if message is None or message.sender_account_id is None:
            return
        account = self.session.get(SenderAccount, message.sender_account_id)
        if account is None:
            return
        from app.services.safety import SenderSafetyService

        service = SenderSafetyService(self.session, self.tenant_id)
        state, reasons = service.evaluate_by_account(account)
        service.apply(account, state, reasons)

    def _append_message_event(
        self,
        message: Message,
        event_type: str,
        provider_event_id: str | None,
        payload: dict[str, Any],
        occurred_at: datetime | None,
    ) -> None:
        message.events.append(
            MessageEvent(
                tenant_id=self.tenant_id,
                event_type=event_type,
                provider_event_id=provider_event_id,
                provider_payload=payload,
                occurred_at=occurred_at or datetime.now(UTC),
            )
        )

    def _bump_campaign_stat(self, message: Message | None, stat: str) -> None:
        if message is None or message.campaign_id is None:
            return
        from app.models import Campaign

        campaign = self.session.get(Campaign, message.campaign_id)
        if campaign is None:
            return
        config = dict(campaign.schedule_config)
        stats = dict(config.get("statistics", {}))
        stats[stat] = int(stats.get(stat, 0)) + 1
        config["statistics"] = stats
        campaign.schedule_config = config
