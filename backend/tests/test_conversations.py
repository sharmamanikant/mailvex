from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Base, Contact, EmailAccount, Reply, Tenant, Thread, User
from app.schemas.conversations import ConversationReplyDecision, ConversationUpdate
from app.services.conversations import (
    ConversationError,
    ConversationNotFoundError,
    ConversationService,
)


@pytest.fixture()
def conversation_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'conversations.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Conversation Tenant", slug=f"conversation-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        sender = EmailAccount(tenant_id=tenant.id, provider="SMTP", email="sender@example.com")
        contact = Contact(tenant_id=tenant.id, email="recipient@example.com")
        user = User(tenant_id=tenant.id, email="agent@example.com", display_name="Agent")
        session.add_all([sender, contact, user])
        session.flush()
        thread = Thread(tenant_id=tenant.id, provider_thread_id="thread-1", sender_id=sender.id, contact_id=contact.id, subject="Hello", status="OPEN", tags=["new"])
        session.add(thread)
        session.flush()
        session.add(Reply(tenant_id=tenant.id, thread_id=thread.id, sender_email=contact.email, recipient_email=sender.email, body_text="Interested", received_at=datetime.now(UTC)))
        session.commit()
        yield session, tenant.id, thread.id, user.id
    engine.dispose()


def test_inbox_and_history_are_tenant_scoped(conversation_session) -> None:
    session, tenant_id, thread_id, _user_id = conversation_session
    service = ConversationService(session, tenant_id)
    threads = service.list()
    assert len(threads) == 1
    assert len(service.get(thread_id).replies) == 1
    with pytest.raises(ConversationNotFoundError):
        ConversationService(session, uuid4()).get(thread_id)


def test_assignment_notes_tags_and_status(conversation_session) -> None:
    session, tenant_id, thread_id, user_id = conversation_session
    thread = ConversationService(session, tenant_id).update(thread_id, ConversationUpdate(assigned_user_id=user_id, notes="Follow up tomorrow", tags=["priority"], status="INTERESTED"))
    assert thread.assigned_user_id == user_id
    assert thread.notes == "Follow up tomorrow"
    assert thread.tags == ["priority"]
    assert thread.status == "INTERESTED"


def test_assignment_cannot_cross_tenant(conversation_session) -> None:
    session, tenant_id, thread_id, _ = conversation_session
    with pytest.raises(ConversationError):
        ConversationService(session, tenant_id).update(thread_id, ConversationUpdate(assigned_user_id=uuid4()))


def test_incoming_reply_is_ai_classified_and_suppresses_unsubscribe(conversation_session) -> None:
    session, tenant_id, thread_id, _ = conversation_session
    service = ConversationService(session, tenant_id)
    reply = service.ingest_reply(
        thread_id,
        "Please unsubscribe me and stop emailing me",
        sender_email="recipient@example.com",
        recipient_email="sender@example.com",
    )
    assert reply.classification == "UNSUBSCRIBE"
    assert reply.suggested_action == "SUPPRESS_CONTACT"
    assert reply.approval_status == "PENDING"
    assert service.session.scalar(service.session.query(type(reply)).filter_by(id=reply.id).statement) is not None
    assert service.session.scalar(service.session.query(type(reply)).filter_by(id=reply.id).statement) is not None


def test_human_approval_is_required_before_sending(conversation_session) -> None:
    session, tenant_id, thread_id, _ = conversation_session
    service = ConversationService(session, tenant_id)
    reply = service.ingest_reply(thread_id, "I am interested in learning more")
    with pytest.raises(ConversationError, match="Human approval"):
        service.process_reply_decision(reply.id, ConversationReplyDecision(approved=False, send=True))
    approved = service.process_reply_decision(reply.id, ConversationReplyDecision(approved=True, send=True))
    assert approved.approval_status == "APPROVED"


def test_unsubscribe_reply_suppresses_contact_and_blocks_send_without_policy(conversation_session) -> None:
    session, tenant_id, thread_id, _ = conversation_session
    service = ConversationService(session, tenant_id)
    reply = service.ingest_reply(
        thread_id,
        "Please unsubscribe me and stop emailing me",
        sender_email="recipient@example.com",
        recipient_email="sender@example.com",
    )

    contact = session.scalar(select(Contact).where(Contact.id == reply.thread.contact_id, Contact.tenant_id == tenant_id))
    assert contact is not None
    assert reply.classification == "UNSUBSCRIBE"
    assert reply.suggested_action == "SUPPRESS_CONTACT"
    assert contact.suppression_status == "UNSUBSCRIBED"

    with pytest.raises(ConversationError, match="Unsubscribe intent requires suppression"):
        service.process_reply_decision(reply.id, ConversationReplyDecision(approved=True, send=True, automation_policy_enabled=False))


def test_approved_unsubscribe_send_is_allowed_only_with_automation_policy(conversation_session) -> None:
    session, tenant_id, thread_id, _ = conversation_session
    service = ConversationService(session, tenant_id)
    reply = service.ingest_reply(
        thread_id,
        "Please unsubscribe me from future communications",
        sender_email="recipient@example.com",
        recipient_email="sender@example.com",
    )

    decision = service.process_reply_decision(
        reply.id,
        ConversationReplyDecision(approved=True, send=True, automation_policy_enabled=True),
    )

    assert decision.approval_status == "APPROVED"
    assert decision.suggested_action == "SUPPRESS_CONTACT"
