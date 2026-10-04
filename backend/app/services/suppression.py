from __future__ import annotations

import hashlib
import secrets
from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import delete, func, select
from sqlalchemy.orm import Session

from app.models import (
    Bounce,
    Complaint,
    Contact,
    Message,
    MessageEvent,
    Suppression,
    Unsubscribe,
)
from app.services.audit import AuditService
from app.services.suppression_engine import SuppressionEngine

SUPPRESSION_REASONS = {"UNSUBSCRIBED", "HARD_BOUNCE", "COMPLAINT", "INVALID", "MANUAL_BLOCK", "POLICY_BLOCK"}


class SuppressionError(ValueError):
    pass


class SuppressionService:
    """Tenant-wide suppression write/read boundary used by compliance and event processors."""

    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    @staticmethod
    def _hash_token(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def is_suppressed(self, email: str) -> bool:
        normalized = email.strip().lower()
        return self.session.scalar(select(Suppression.id).where(Suppression.tenant_id == self.tenant_id, Suppression.email == normalized)) is not None

    def list_suppressions(self, search: str | None = None, reason: str | None = None) -> list[Suppression]:
        statement = select(Suppression).where(Suppression.tenant_id == self.tenant_id)
        if search:
            statement = statement.where(Suppression.email.ilike(f"%{search.strip()}%"))
        if reason:
            statement = statement.where(Suppression.reason == reason)
        return list(self.session.scalars(statement.order_by(Suppression.effective_at.desc())).all())

    def remove_suppression(self, suppression_id: UUID) -> None:
        record = self.session.scalar(select(Suppression).where(Suppression.id == suppression_id, Suppression.tenant_id == self.tenant_id))
        if record is None:
            raise SuppressionError("Suppression not found")
        contact = self.session.scalar(select(Contact).where(Contact.tenant_id == self.tenant_id, Contact.email == record.email))
        if contact is not None and contact.suppression_status == record.reason:
            contact.suppression_status = "CLEAR"
        self.session.execute(delete(Suppression).where(Suppression.id == suppression_id, Suppression.tenant_id == self.tenant_id))
        self.session.commit()

    def suppress(self, email: str, reason: str, source: str, contact_id: UUID | None = None) -> Suppression:
        if reason not in SUPPRESSION_REASONS:
            raise SuppressionError("Invalid suppression reason")
        normalized = email.strip().lower()
        existing = self.session.scalar(select(Suppression).where(Suppression.tenant_id == self.tenant_id, Suppression.email == normalized))
        if existing is not None:
            return existing
        record = Suppression(tenant_id=self.tenant_id, email=normalized, contact_id=contact_id, reason=reason, source=source, effective_at=datetime.now(UTC))
        self.session.add(record)
        contact = self.session.scalar(select(Contact).where(Contact.tenant_id == self.tenant_id, Contact.id == contact_id)) if contact_id else self.session.scalar(select(Contact).where(Contact.tenant_id == self.tenant_id, Contact.email == normalized))
        if contact is not None:
            contact.suppression_status = reason
        self.session.flush()
        return record

    def create_unsubscribe_token(self, email: str, contact_id: UUID | None = None, campaign_id: UUID | None = None) -> str:
        token = secrets.token_urlsafe(48)
        self.session.add(Unsubscribe(tenant_id=self.tenant_id, email=email.strip().lower(), contact_id=contact_id, campaign_id=campaign_id, token_hash=self._hash_token(token), unsubscribed_at=datetime.now(UTC)))
        self.session.commit()
        return token

    def consume_unsubscribe_token(self, token: str) -> Suppression:
        record = self.session.scalar(select(Unsubscribe).where(Unsubscribe.token_hash == self._hash_token(token), Unsubscribe.tenant_id == self.tenant_id))
        if record is None:
            raise SuppressionError("Invalid unsubscribe token")
        suppression = self.suppress(record.email, "UNSUBSCRIBED", "unsubscribe", record.contact_id)
        # Phase 14: write the authoritative safety control so every send gate
        # (which consults suppression_entries) honors the unsubscribe, and
        # record the UNSUBSCRIBE audit event.
        SuppressionEngine(self.session, self.tenant_id).suppress(
            record.email,
            "UNSUBSCRIBED",
            source="unsubscribe",
            reason="Recipient unsubscribed",
            campaign_id=record.campaign_id,
            contact_id=record.contact_id,
        )
        AuditService(self.session, self.tenant_id).record(
            "UNSUBSCRIBE", "suppression", suppression.id, {"email": record.email}
        )
        message_id = self._message_for_campaign(record.campaign_id)
        if message_id is not None:
            self.session.add(MessageEvent(tenant_id=self.tenant_id, message_id=message_id, event_type="UNSUBSCRIBED", provider_payload={"source": "unsubscribe"}, occurred_at=datetime.now(UTC)))
        self.session.commit()
        return suppression

    def build_unsubscribe_url(self, email: str, contact_id: UUID | None = None, campaign_id: UUID | None = None) -> str:
        """Create a signed, unpredictable unsubscribe URL for a recipient.

        The token is high-entropy and hashed at rest (never stored in the URL);
        it does not expose contact IDs or any internal database identity.
        """
        token = self.create_unsubscribe_token(email, contact_id, campaign_id)
        from app.core.config import settings

        base = settings.public_base_url.rstrip("/")
        return f"{base}/unsubscribe/{token}"

    def process_bounce(self, message_id: UUID, classification: str, diagnostic: str | None = None, repeated_failure_threshold: int = 3) -> str:
        message = self.session.scalar(select(Message).where(Message.id == message_id, Message.tenant_id == self.tenant_id))
        if message is None:
            raise SuppressionError("Message not found")
        bounce = Bounce(tenant_id=self.tenant_id, message_id=message.id, classification=classification, diagnostic=diagnostic, occurred_at=datetime.now(UTC))
        self.session.add(bounce)
        message.status = "BOUNCED"
        self.session.add(MessageEvent(tenant_id=self.tenant_id, message_id=message.id, event_type="BOUNCED", provider_payload={"classification": classification}, occurred_at=datetime.now(UTC)))
        if classification == "HARD_BOUNCE":
            self.suppress(self._message_email(message), "HARD_BOUNCE", "bounce", message.contact_id)
            result = "SUPPRESSED"
        elif classification == "TEMPORARY_FAILURE":
            count = self.session.scalar(select(func.count(Bounce.id)).where(Bounce.tenant_id == self.tenant_id, Bounce.message_id == message.id)) or 0
            result = "SUPPRESSED" if count >= repeated_failure_threshold else "RETRY"
            if result == "SUPPRESSED":
                self.suppress(self._message_email(message), "POLICY_BLOCK", "repeated-temporary-failure", message.contact_id)
        else:
            result = "RECORDED"
        self.session.commit()
        return result

    def process_complaint(self, message_id: UUID, provider_reference: str | None = None) -> None:
        message = self.session.scalar(select(Message).where(Message.id == message_id, Message.tenant_id == self.tenant_id))
        if message is None:
            raise SuppressionError("Message not found")
        self.session.add(Complaint(tenant_id=self.tenant_id, message_id=message.id, provider_reference=provider_reference, occurred_at=datetime.now(UTC)))
        self.suppress(self._message_email(message), "COMPLAINT", "provider-complaint", message.contact_id)
        self.session.add(MessageEvent(tenant_id=self.tenant_id, message_id=message.id, event_type="COMPLAINT", provider_payload={"provider_reference": provider_reference}, occurred_at=datetime.now(UTC)))
        self.session.commit()

    def _message_email(self, message: Message) -> str:
        contact = self.session.scalar(select(Contact).where(Contact.id == message.contact_id, Contact.tenant_id == self.tenant_id))
        if contact is None:
            raise SuppressionError("Message recipient not found")
        return contact.email

    def _message_for_campaign(self, campaign_id: UUID | None) -> UUID | None:
        if campaign_id is None:
            return None
        return self.session.scalar(select(Message.id).where(Message.tenant_id == self.tenant_id, Message.campaign_id == campaign_id).order_by(Message.created_at.desc()))
