from __future__ import annotations

from datetime import UTC, datetime
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session, selectinload

from app.models import Contact, Reply, Thread, User
from app.providers.mock_ai import MockAIProvider
from app.schemas.conversations import ConversationReplyDecision, ConversationUpdate


class ConversationNotFoundError(LookupError):
    pass


class ConversationError(ValueError):
    pass


class ConversationService:
    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.ai = MockAIProvider()

    def _thread(self, thread_id: UUID) -> Thread:
        thread = self.session.scalar(select(Thread).options(selectinload(Thread.replies)).where(Thread.id == thread_id, Thread.tenant_id == self.tenant_id))
        if thread is None:
            raise ConversationNotFoundError("Conversation not found")
        return thread

    def list(self, status: str | None = None, assigned_user_id: UUID | None = None) -> list[Thread]:
        filters = [Thread.tenant_id == self.tenant_id]
        if status:
            filters.append(Thread.status == status)
        if assigned_user_id:
            filters.append(Thread.assigned_user_id == assigned_user_id)
        return list(self.session.scalars(select(Thread).options(selectinload(Thread.replies)).where(*filters).order_by(Thread.last_message_at.desc().nullslast(), Thread.updated_at.desc())).all())

    def get(self, thread_id: UUID) -> Thread:
        return self._thread(thread_id)

    def update(self, thread_id: UUID, payload: ConversationUpdate) -> Thread:
        thread = self._thread(thread_id)
        values = payload.model_dump(exclude_unset=True)
        if "assigned_user_id" in values and values["assigned_user_id"] is not None:
            user = self.session.scalar(select(User.id).where(User.id == values["assigned_user_id"], User.tenant_id == self.tenant_id, User.status == "ACTIVE"))
            if user is None:
                raise ConversationError("Assignee is not available in this tenant")
        for key, value in values.items():
            setattr(thread, key, value)
        self.session.commit()
        return self._thread(thread.id)

    def add_reply_draft(self, thread_id: UUID, body_text: str) -> Reply:
        thread = self._thread(thread_id)
        reply = Reply(tenant_id=self.tenant_id, thread_id=thread.id, body_text=body_text, approval_status="DRAFT", received_at=datetime.now(UTC))
        self.session.add(reply)
        self.session.commit()
        return reply

    def ingest_reply(self, thread_id: UUID, body_text: str, sender_email: str | None = None, recipient_email: str | None = None) -> Reply:
        thread = self._thread(thread_id)
        classification = self.ai.classify_reply(body_text)
        classification_name = str(classification.get("classification", "OTHER")).upper()
        action_map = {
            "INTERESTED": "SCHEDULE_FOLLOW_UP",
            "NOT_INTERESTED": "MARK_NOT_INTERESTED",
            "NEEDS_INFORMATION": "SEND_DETAILS",
            "MEETING_REQUEST": "SCHEDULE_MEETING",
            "SEND_DETAILS": "SEND_DETAILS",
            "WRONG_PERSON": "FORWARD_TO_RIGHT_CONTACT",
            "OUT_OF_OFFICE": "RETRY_LATER",
            "UNSUBSCRIBE": "SUPPRESS_CONTACT",
            "OTHER": "REVIEW_MANUALLY",
        }
        suggested_action = action_map.get(classification_name, "REVIEW_MANUALLY")
        reply = Reply(
            tenant_id=self.tenant_id,
            thread_id=thread.id,
            sender_email=sender_email or (thread.sender_id and self.session.scalar(select(Contact.email).where(Contact.id == thread.sender_id, Contact.tenant_id == self.tenant_id))),
            recipient_email=recipient_email or (thread.contact_id and self.session.scalar(select(Contact.email).where(Contact.id == thread.contact_id, Contact.tenant_id == self.tenant_id))),
            body_text=body_text,
            classification=classification_name,
            suggested_action=suggested_action,
            suggested_response=self.ai.generate_reply(body_text, {"classification": classification_name}),
            approval_status="PENDING",
            received_at=datetime.now(UTC),
        )
        self.session.add(reply)
        if classification_name == "UNSUBSCRIBE":
            from app.services.suppression import SuppressionService
            target_email = reply.recipient_email or (thread.contact_id and self.session.scalar(select(Contact.email).where(Contact.id == thread.contact_id, Contact.tenant_id == self.tenant_id)))
            if target_email:
                SuppressionService(self.session, self.tenant_id).suppress(target_email, "UNSUBSCRIBED", "reply-unsubscribe", thread.contact_id)
        self.session.commit()
        return self.session.scalar(select(Reply).where(Reply.id == reply.id, Reply.tenant_id == self.tenant_id))

    def process_reply_decision(self, reply_id: UUID, decision: ConversationReplyDecision) -> Reply:
        reply = self.session.scalar(select(Reply).where(Reply.id == reply_id, Reply.tenant_id == self.tenant_id))
        if reply is None:
            raise ConversationNotFoundError("Conversation reply not found")
        if decision.edited_response is not None:
            reply.suggested_response = decision.edited_response
        if decision.send and not decision.approved and not decision.automation_policy_enabled:
            raise ConversationError("Human approval is required before any reply is sent.")
        if not decision.approved and not decision.send:
            reply.approval_status = "REJECTED"
            self.session.commit()
            return reply
        if decision.send and reply.classification == "UNSUBSCRIBE" and not decision.automation_policy_enabled:
            raise ConversationError("Unsubscribe intent requires suppression and cannot be sent without an explicit automation policy.")
        reply.approval_status = "APPROVED" if decision.approved else "REJECTED"
        self.session.commit()
        return reply
