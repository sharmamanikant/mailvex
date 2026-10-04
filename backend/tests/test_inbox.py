from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.database import get_db
from app.main import app
from app.models import (
    Base,
    Campaign,
    Contact,
    EmailAccount,
    EmailThread,
    Permission,
    Role,
    RolePermission,
    Suppression,
    Template,
    TemplateVersion,
    Tenant,
    User,
    UserRole,
)
from app.providers import (
    MockEmailProvider,
    ProviderInboxMessage,
    ProviderProfile,
)
from app.security.passwords import hash_password
from app.services.inbox import InboxService
from app.services.inbox_reply import InboxReplyService


def _profile(email: str) -> ProviderProfile:
    return ProviderProfile(email=email)


def _item(
    message_id: str,
    thread_id: str,
    *,
    from_email: str,
    to_email: str,
    subject: str = "Re: Hello",
    body: str = "Interested, tell me more",
    received_at: datetime | None = None,
    direction: str = "INBOUND",
) -> ProviderInboxMessage:
    return ProviderInboxMessage(
        id=message_id,
        thread_id=thread_id,
        direction=direction,
        from_email=from_email,
        to_email=to_email,
        subject=subject,
        body_text=body,
        received_at=received_at or datetime.now(UTC),
    )


@pytest.fixture()
def db(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'inbox.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    tenant = Tenant(name="T", slug=f"t-{uuid4().hex[:8]}")
    session.add(tenant)
    session.flush()
    sender = EmailAccount(
        tenant_id=tenant.id, provider="GOOGLE", email="sender@example.com", status="CONNECTED"
    )
    session.add(sender)
    session.flush()
    session.commit()
    return session, tenant, sender, session_factory


# ------------------------------------------------------------------ sync


def test_sync_creates_threads_and_messages(db) -> None:
    session, tenant, sender, _ = db
    mock = MockEmailProvider("GOOGLE", _profile(sender.email))
    mock.inbox = [
        _item("m1", "t1", from_email="a@example.com", to_email=sender.email, body="hi"),
        _item("m2", "t1", from_email="a@example.com", to_email=sender.email, body="follow up"),
    ]
    service = InboxService(session, tenant.id, provider_factory=lambda s: mock)

    result = service.sync_sender(sender.id)

    assert result["new_threads"] == 1
    assert result["new_messages"] == 2
    threads, total = service.list_threads()
    assert total == 1
    thread = service.get_thread(threads[0].id)
    assert len(thread.messages) == 2
    assert thread.subject == "Re: Hello"
    assert thread.status == "UNREAD"


def test_sync_deduplicates_messages(db) -> None:
    session, tenant, sender, _ = db
    mock = MockEmailProvider("GOOGLE", _profile(sender.email))
    mock.inbox = [
        _item("m1", "t1", from_email="a@example.com", to_email=sender.email),
        _item("m2", "t1", from_email="a@example.com", to_email=sender.email),
    ]
    service = InboxService(session, tenant.id, provider_factory=lambda s: mock)

    service.sync_sender(sender.id)
    first = service.sync_sender(sender.id)

    assert first["new_threads"] == 0
    assert first["new_messages"] == 0
    threads, total = service.list_threads()
    assert total == 1
    assert len(service.get_thread(threads[0].id).messages) == 2


def test_sync_separates_threads(db) -> None:
    session, tenant, sender, _ = db
    mock = MockEmailProvider("GOOGLE", _profile(sender.email))
    mock.inbox = [
        _item("m1", "tA", from_email="a@example.com", to_email=sender.email),
        _item("m2", "tB", from_email="b@example.com", to_email=sender.email),
    ]
    service = InboxService(session, tenant.id, provider_factory=lambda s: mock)
    service.sync_sender(sender.id)
    threads, total = service.list_threads()
    assert total == 2
    assert {t.external_thread_id for t in threads} == {"tA", "tB"}


def test_sync_provider_failure_is_surfaced(db) -> None:
    session, tenant, sender, _ = db
    mock = MockEmailProvider("GOOGLE", _profile(sender.email))
    mock.fail_sync = True
    service = InboxService(session, tenant.id, provider_factory=lambda s: mock)
    from app.services.inbox import InboxSyncError

    with pytest.raises(InboxSyncError):
        service.sync_sender(sender.id)


def test_sync_smtp_not_supported(db) -> None:
    from app.services.inbox import InboxNotSupportedError

    session, tenant, sender, _ = db
    sender.provider = "SMTP"
    session.commit()
    mock = MockEmailProvider("SMTP", _profile(sender.email))
    mock.supports_inbox = False
    service = InboxService(session, tenant.id, provider_factory=lambda s: mock)

    with pytest.raises(InboxNotSupportedError):
        service.sync_sender(sender.id)


# ------------------------------------------------------------- association


def test_reply_associated_with_contact_and_campaign(db) -> None:
    session, tenant, sender, _ = db
    contact = Contact(tenant_id=tenant.id, email="a@example.com", first_name="Alice")
    campaign = Campaign(
        tenant_id=tenant.id, name="Camp", objective="Launch", sender_id=sender.id
    )
    session.add_all([contact, campaign])
    session.flush()
    template = Template(tenant_id=tenant.id, name="T")
    session.add(template)
    session.flush()
    version = TemplateVersion(
        tenant_id=tenant.id, template_id=template.id, version_number=1,
        subject_template="Hi", html_body="<p>x</p>", text_body="x",
    )
    session.add(version)
    session.flush()
    campaign.template_version_id = version.id
    from app.models import Message

    session.add(
        Message(
            tenant_id=tenant.id, campaign_id=campaign.id, sender_id=sender.id,
            contact_id=contact.id, subject="Hello", status="SENT",
        )
    )
    session.commit()

    mock = MockEmailProvider("GOOGLE", _profile(sender.email))
    mock.inbox = [_item("m1", "t1", from_email="a@example.com", to_email=sender.email)]
    service = InboxService(session, tenant.id, provider_factory=lambda s: mock)
    service.sync_sender(sender.id)
    threads, _ = service.list_threads()
    thread = service.get_thread(threads[0].id)

    assert thread.contact_id == contact.id
    assert thread.campaign_id == campaign.id
    assert thread.match_status == "MATCHED"


def test_unmatched_reply_keeps_unknown_association(db) -> None:
    session, tenant, sender, _ = db
    # No contact with this email, and multiple contacts -> low confidence.
    session.add(Contact(tenant_id=tenant.id, email="other@example.com"))
    session.commit()
    mock = MockEmailProvider("GOOGLE", _profile(sender.email))
    mock.inbox = [_item("m1", "t1", from_email="nobody@example.com", to_email=sender.email)]
    service = InboxService(session, tenant.id, provider_factory=lambda s: mock)
    service.sync_sender(sender.id)
    threads, _ = service.list_threads()
    thread = service.get_thread(threads[0].id)
    assert thread.contact_id is None
    assert thread.match_status == "UNMATCHED"


def test_ambiguous_campaign_keeps_contact_but_not_campaign(db) -> None:
    session, tenant, sender, _ = db
    contact = Contact(tenant_id=tenant.id, email="a@example.com")
    c1 = Campaign(tenant_id=tenant.id, name="C1", objective="x", sender_id=sender.id)
    c2 = Campaign(tenant_id=tenant.id, name="C2", objective="y", sender_id=sender.id)
    session.add_all([contact, c1, c2])
    session.flush()
    from app.models import Message

    session.add_all([
        Message(tenant_id=tenant.id, campaign_id=c1.id, sender_id=sender.id, contact_id=contact.id, subject="a", status="SENT"),
        Message(tenant_id=tenant.id, campaign_id=c2.id, sender_id=sender.id, contact_id=contact.id, subject="b", status="SENT"),
    ])
    session.commit()
    mock = MockEmailProvider("GOOGLE", _profile(sender.email))
    mock.inbox = [_item("m1", "t1", from_email="a@example.com", to_email=sender.email)]
    service = InboxService(session, tenant.id, provider_factory=lambda s: mock)
    service.sync_sender(sender.id)
    threads, _ = service.list_threads()
    thread = service.get_thread(threads[0].id)
    assert thread.contact_id == contact.id
    assert thread.campaign_id is None


# --------------------------------------------------------------- read ops


def test_status_transition(db) -> None:
    session, tenant, sender, _ = db
    mock = MockEmailProvider("GOOGLE", _profile(sender.email))
    mock.inbox = [_item("m1", "t1", from_email="a@example.com", to_email=sender.email)]
    service = InboxService(session, tenant.id, provider_factory=lambda s: mock)
    service.sync_sender(sender.id)
    threads, _ = service.list_threads()
    thread = service.set_status(threads[0].id, "REQUIRES_ACTION")
    assert thread.status == "REQUIRES_ACTION"

    from app.services.inbox import InboxError

    with pytest.raises(InboxError):
        service.set_status(threads[0].id, "BOGUS")


def test_recipient_context_returns_details(db) -> None:
    session, tenant, sender, _ = db
    contact = Contact(
        tenant_id=tenant.id, email="a@example.com", first_name="Alice",
        last_name="Smith", company="Acme", designation="CTO",
    )
    campaign = Campaign(tenant_id=tenant.id, name="Camp", objective="o", sender_id=sender.id)
    session.add_all([contact, campaign])
    session.flush()
    session.add(
        Contact(tenant_id=tenant.id, email="b@example.com")
    )
    session.commit()
    from app.services.inbox import InboxService as Svc

    svc = Svc(session, tenant.id)
    thread = EmailThread(
        tenant_id=tenant.id, sender_id=sender.id, external_thread_id="t1",
        subject="Re", status="UNREAD", provider="GOOGLE",
        contact_id=contact.id, campaign_id=campaign.id, match_status="MATCHED",
    )
    session.add(thread)
    session.commit()
    ctx = svc.recipient_context(thread)
    assert ctx is not None
    assert ctx["name"] == "Alice Smith"
    assert ctx["company"] == "Acme"
    assert ctx["campaign_name"] == "Camp"


# ------------------------------------------------------------------ replies


def test_ai_reply_draft_is_never_sent(db) -> None:
    session, tenant, sender, _ = db
    mock = MockEmailProvider("GOOGLE", _profile(sender.email))
    mock.inbox = [_item("m1", "t1", from_email="a@example.com", to_email=sender.email)]
    service = InboxService(session, tenant.id, provider_factory=lambda s: mock)
    service.sync_sender(sender.id)
    threads, _ = service.list_threads()
    thread = service.get_thread(threads[0].id)

    reply = InboxReplyService(session, tenant.id, provider_factory=lambda s: mock)
    draft = reply.draft(thread.id)

    assert draft.status == "DRAFT"
    assert draft.body
    # Nothing was sent through the provider.
    assert mock.sent == []
    # Thread status unchanged (draft alone never changes it).
    assert service.get_thread(thread.id).status == "UNREAD"

    approved = reply.approve(draft.id)
    assert approved.status == "APPROVED"
    assert mock.sent == []


def test_manual_reply_sends_and_marks_replied(db) -> None:
    session, tenant, sender, _ = db
    mock = MockEmailProvider("GOOGLE", _profile(sender.email))
    mock.inbox = [_item("m1", "t1", from_email="a@example.com", to_email=sender.email)]
    service = InboxService(session, tenant.id, provider_factory=lambda s: mock)
    service.sync_sender(sender.id)
    threads, _ = service.list_threads()
    thread = service.get_thread(threads[0].id)

    reply = InboxReplyService(session, tenant.id, provider_factory=lambda s: mock)
    outgoing = reply.send(thread.id, "Re: Hello", "Thanks! Here are details.")

    assert outgoing.direction == "OUTBOUND"
    assert outgoing.status == "SENT"
    # The mock appended to provider.sent list.
    assert len(mock.sent) == 1
    assert service.get_thread(thread.id).status == "REPLIED"


def test_reply_to_suppressed_recipient_is_blocked(db) -> None:
    session, tenant, sender, _ = db
    session.add(Suppression(tenant_id=tenant.id, email="a@example.com", reason="COMPLAINT", source="test", effective_at=datetime.now(UTC)))
    session.commit()
    mock = MockEmailProvider("GOOGLE", _profile(sender.email))
    mock.inbox = [_item("m1", "t1", from_email="a@example.com", to_email=sender.email)]
    service = InboxService(session, tenant.id, provider_factory=lambda s: mock)
    service.sync_sender(sender.id)
    threads, _ = service.list_threads()
    thread = service.get_thread(threads[0].id)

    reply = InboxReplyService(session, tenant.id, provider_factory=lambda s: mock)
    from app.services.inbox_reply import InboxReplyBlockedError

    with pytest.raises(InboxReplyBlockedError):
        reply.send(thread.id, "Re: Hello", "Thanks")


def test_send_requires_content(db) -> None:
    session, tenant, sender, _ = db
    mock = MockEmailProvider("GOOGLE", _profile(sender.email))
    mock.inbox = [_item("m1", "t1", from_email="a@example.com", to_email=sender.email)]
    service = InboxService(session, tenant.id, provider_factory=lambda s: mock)
    service.sync_sender(sender.id)
    threads, _ = service.list_threads()
    thread = threads[0]
    reply = InboxReplyService(session, tenant.id, provider_factory=lambda s: mock)
    from app.services.inbox_reply import InboxReplyError

    with pytest.raises(InboxReplyError):
        reply.send(thread.id, "", "body")
    with pytest.raises(InboxReplyError):
        reply.send(thread.id, "subject", "")


# ------------------------------------------------------------------- API


@pytest.fixture()
def api_client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'api.db'}", connect_args={"check_same_thread": False}
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        db_session = session_factory()
        try:
            yield db_session
        finally:
            db_session.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)

    session = session_factory()
    tenant = Tenant(name="T", slug=f"t-{uuid4().hex[:8]}")
    session.add(tenant)
    session.flush()
    user = User(
        tenant_id=tenant.id,
        email="owner@example.com",
        password_hash=hash_password("correct horse battery staple"),
        display_name="Owner",
    )
    role = Role(tenant_id=tenant.id, name="Admin")
    permission = Permission(key="analytics.read", description="Read")
    session.add_all([user, role, permission])
    session.flush()
    session.add_all([
        UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id),
        RolePermission(role_id=role.id, permission_id=permission.id),
    ])
    sender = EmailAccount(
        tenant_id=tenant.id, provider="GOOGLE", email="sender@example.com", status="CONNECTED"
    )
    session.add(sender)
    session.flush()
    thread = EmailThread(
        tenant_id=tenant.id, sender_id=sender.id, external_thread_id="t1",
        subject="Re: Hello", status="UNREAD", provider="GOOGLE",
    )
    session.add(thread)
    session.commit()
    session.close()

    login = client.post(
        "/api/v1/auth/login",
        json={"email": "owner@example.com", "password": "correct horse battery staple"},
    )
    token = login.json()["access_token"]
    yield client, token, tenant.id, thread.id
    app.dependency_overrides.clear()
    engine.dispose()


def test_api_requires_authentication(api_client) -> None:
    client, _token, _tenant, _thread_id = api_client
    response = client.get("/api/v1/inbox/threads")
    assert response.status_code == 401


def test_api_lists_threads(api_client) -> None:
    client, token, _tenant, _thread_id = api_client
    response = client.get("/api/v1/inbox/threads", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 200
    payload = response.json()
    assert payload["total"] >= 1
    assert payload["items"][0]["sender_email"] == "sender@example.com"


def test_api_get_thread(api_client) -> None:
    client, token, _tenant, thread_id = api_client
    response = client.get(
        f"/api/v1/inbox/threads/{thread_id}",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["subject"] == "Re: Hello"


def test_api_set_status(api_client) -> None:
    client, token, _tenant, thread_id = api_client
    response = client.post(
        f"/api/v1/inbox/threads/{thread_id}/status",
        json={"status": "README"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 400

    response = client.post(
        f"/api/v1/inbox/threads/{thread_id}/status",
        json={"status": "READ"},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    assert response.json()["status"] == "READ"


def test_threads_are_tenant_isolated(db) -> None:
    from app.models import EmailAccount
    from app.models import Tenant as TenantModel

    session, tenant, sender, _ = db
    other_tenant = TenantModel(name="Other", slug=f"o-{uuid4().hex[:8]}")
    session.add(other_tenant)
    session.flush()
    other_sender = EmailAccount(
        tenant_id=other_tenant.id, provider="GOOGLE", email="other@example.com", status="CONNECTED"
    )
    session.add(other_sender)
    session.flush()
    session.add(
        EmailThread(
            tenant_id=other_tenant.id, sender_id=other_sender.id,
            external_thread_id="t-other", subject="Other", status="UNREAD", provider="GOOGLE",
        )
    )
    session.commit()

    mock = MockEmailProvider("GOOGLE", _profile(sender.email))
    mock.inbox = [_item("m1", "t1", from_email="a@example.com", to_email=sender.email)]
    service = InboxService(session, tenant.id, provider_factory=lambda s: mock)
    service.sync_sender(sender.id)

    threads, total = service.list_threads()
    # Only the current tenant's thread is visible.
    assert total == 1
    assert all(t.tenant_id == tenant.id for t in threads)
    # The current tenant cannot read another tenant's thread.
    other_thread = session.scalar(
        select(EmailThread).where(EmailThread.subject == "Other")
    )
    from app.services.inbox import InboxNotFoundError

    with pytest.raises(InboxNotFoundError):
        service.get_thread(other_thread.id)
