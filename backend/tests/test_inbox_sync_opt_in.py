"""STEP 13 inbox/reply sync (opt-in mailbox scope) tests.

Covers the System B inbox-sync contract end to end:

* Google ``sync_inbox`` parses the normalized dict contract once
  ``gmail.readonly`` is granted (and stays scope-guarded without it).
* Microsoft ``sync_inbox`` does the same for ``Mail.Read``.
* Reply-sync enabling is opt-in: it refuses send-only connections and
  succeeds only when the connection's consent actually granted the mailbox
  scope.
* Reply linking matches the self-supplied RFC 5322 ``Message-Id`` stored at
  send time (sending.py) as well as the provider message id.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import Engine, create_engine
from sqlalchemy.orm import Session

from app.core.config import settings
from app.email_providers import (
    ProviderConnectionConfig,
    encrypt_credential_reference,
    get_provider,
)
from app.email_providers.base import EmailProviderError, ProviderErrorCode
from app.email_providers.google.provider import GMAIL_INBOX_SCOPE
from app.email_providers.microsoft.provider import (
    GRAPH_BASE,
    MAIL_READ_SCOPE,
    MicrosoftEmailProvider,
)
from app.models import (
    Base,
    Campaign,
    Contact,
    EmailAccount,
    Message,
    SenderAccount,
    SenderConnection,
    Tenant,
)
from app.services.integrations import IntegrationService, IntegrationValidationError
from app.services.reply_sync import ReplySyncService


def _reference(payload: dict[str, object]) -> str:
    return encrypt_credential_reference(settings.encryption_key, payload)


def _google_inbox_config() -> ProviderConnectionConfig:
    return ProviderConnectionConfig(
        connection_type="OAUTH",
        external_account_id="uboo-face",
        email="sender@example.com",
        metadata={"scopes": ["openid", "https://www.googleapis.com/auth/gmail.send", GMAIL_INBOX_SCOPE]},
        credential_reference=_reference(
            {
                "access_token": "ya29.inbox",
                "refresh_token": "1//inbox",
                "client_id": "client",
                "token_uri": "https://oauth2.googleapis.com/token",
                "scopes": [GMAIL_INBOX_SCOPE],
            }
        ),
    )


def _ms_inbox_config() -> ProviderConnectionConfig:
    return ProviderConnectionConfig(
        connection_type="OAUTH",
        external_account_id="user@example.com",
        email="other@example.com",
        metadata={"scopes": ["openid", "profile", "User.Read", "Mail.Send", MAIL_READ_SCOPE]},
        credential_reference=_reference(
            {
                "access_token": "ms-token",
                "refresh_token": "ms-refresh",
                "client_id": settings.microsoft_client_id or "client",
                "scopes": [MAIL_READ_SCOPE],
            }
        ),
    )


# ------------------------------------------------------------------ #
# Provider inbox sync — parsing under granted opt-in scope
# ------------------------------------------------------------------ #
def test_google_sync_inbox_parses_reply_with_granted_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = get_provider("GOOGLE")
    payload: dict[str, object] = {
        "id": "m1",
        "threadId": "t1",
        "internalDate": "1700000000000",
        "payload": {
            "headers": [
                {"name": "From", "value": "leads@example.com"},
                {"name": "To", "value": "sender@example.com"},
                {"name": "Subject", "value": "Re: Your campaign"},
                {"name": "Message-ID", "value": "<leads-msg-1@example.com>"},
                {"name": "In-Reply-To", "value": "<cb4c7c74-sent@example.com>"},
                {"name": "References", "value": "<cb4c7c74-sent@example.com> <other@example.com>"},
            ],
            "body": {"data": "UmVwbHkgdGV4dA=="},  # "Reply text"
        },
    }

    class _FakeList:
        def execute(self) -> dict[str, object]:
            return {"messages": [{"id": "m1", "threadId": "t1"}]}

    class _FakeGet:
        def execute(self) -> dict[str, object]:
            return payload

    class _FakeMessages:
        def list(self, **kwargs: object) -> _FakeList:
            assert kwargs["q"] == "in:inbox"
            assert kwargs.get("maxResults") == 6
            return _FakeList()

        def get(self, **kwargs: object) -> _FakeGet:
            assert kwargs["format"] == "full"
            return _FakeGet()

    class _FakeUsers:
        def messages(self) -> _FakeMessages:
            return _FakeMessages()

    class _FakeService:
        def users(self) -> _FakeUsers:
            return _FakeUsers()

    monkeypatch.setattr(provider, "_build_service", lambda config: _FakeService())

    inbound = provider.sync_inbox(_google_inbox_config(), limit=5)
    assert len(inbound) == 1
    assert inbound[0]["id"] == "m1"
    assert inbound[0]["thread_id"] == "t1"
    assert inbound[0]["message_id"] == "<leads-msg-1@example.com>"
    assert inbound[0]["in_reply_to"] == "<cb4c7c74-sent@example.com>"
    assert isinstance(inbound[0]["references"], str)
    assert "cb4c7c74-sent@example.com" in inbound[0]["references"]
    assert inbound[0]["from"] == "leads@example.com"
    assert inbound[0]["direction"] == "INBOUND"
    assert inbound[0]["body_preview"] == "Reply text"


def test_google_sync_inbox_still_scope_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = get_provider("GOOGLE")
    monkeypatch.setattr(
        provider, "_build_service", lambda config: pytest.fail("must not touch the API without scope")
    )
    send_only = _google_inbox_config()
    send_only = ProviderConnectionConfig(
        connection_type=send_only.connection_type,
        external_account_id=send_only.external_account_id,
        email=send_only.email,
        metadata={},  # no granted mailbox scope
        credential_reference=send_only.credential_reference,
    )
    with pytest.raises(EmailProviderError) as exc_info:
        provider.sync_inbox(send_only)
    assert exc_info.value.code == ProviderErrorCode.PERMISSION_DENIED


def test_microsoft_sync_inbox_parses_reply_with_granted_scope(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = get_provider("MICROSOFT")
    assert isinstance(provider, MicrosoftEmailProvider)

    def _fake_token(config: ProviderConnectionConfig) -> dict[str, object]:
        return {"access_token": "ms-token"}

    monkeypatch.setattr(provider, "_token", _fake_token)
    response = Mock()
    response.ok = True
    response.status_code = 200
    response.json.return_value = {
        "value": [
            {
                "id": "inbox-1",
                "conversationId": "thread-1",
                "subject": "Re: followup",
                "from": {"emailAddress": {"address": "leads@example.com"}},
                "receivedDateTime": "2026-01-01T12:00:00Z",
                "bodyPreview": "Quick reply",
                "internetMessageId": "<leads-inbox@example.com>",
                "internetMessageHeaders": [
                    {"name": "In-Reply-To", "value": "<cb4c7c74-sent@example.com>"},
                    {"name": "References", "value": "<cb4c7c74-sent@example.com> <x@example.com>"},
                ],
            }
        ]
    }
    captured: dict[str, Any] = {}

    def _fake_get(url: str, **kwargs: object) -> Mock:
        captured["url"] = url
        captured["params"] = kwargs.get("params")
        return response

    monkeypatch.setattr("app.email_providers.microsoft.provider.requests.get", _fake_get)

    inbound = provider.sync_inbox(_ms_inbox_config(), limit=5)
    assert captured["url"] == f"{GRAPH_BASE}/me/mailFolders/inbox/messages"
    assert captured["params"]["$top"] == "5"
    assert "internetMessageHeaders" in captured["params"]["$select"]
    assert len(inbound) == 1
    assert inbound[0]["id"] == "inbox-1"
    assert inbound[0]["thread_id"] == "thread-1"
    assert inbound[0]["message_id"] == "<leads-inbox@example.com>"
    assert inbound[0]["in_reply_to"] == "<cb4c7c74-sent@example.com>"
    assert inbound[0]["from"] == "leads@example.com"
    assert inbound[0]["direction"] == "INBOUND"
    assert inbound[0]["body_preview"] == "Quick reply"


def test_microsoft_sync_inbox_still_scope_guarded(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = get_provider("MICROSOFT")
    send_only = _ms_inbox_config()
    send_only = ProviderConnectionConfig(
        connection_type=send_only.connection_type,
        external_account_id=send_only.external_account_id,
        email=send_only.email,
        metadata={},  # mailboxes absent
        credential_reference=send_only.credential_reference,
    )
    with pytest.raises(EmailProviderError) as exc_info:
        provider.sync_inbox(send_only)
    assert exc_info.value.code == ProviderErrorCode.PERMISSION_DENIED


# ------------------------------------------------------------------ #
# Opt-in reply-sync service + linking (DB-backed)
# ------------------------------------------------------------------ #
def _inbox_bundle() -> tuple[Engine, Session, Tenant, SenderConnection, SenderAccount]:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    tenant = Tenant(name="Inbox Tenant", slug=f"inbox-{uuid4().hex[:8]}")
    session.add(tenant)
    session.flush()
    connection = SenderConnection(
        tenant_id=tenant.id,
        provider="MICROSOFT",
        connection_type="OAUTH",
        status="CONNECTED",
        email="sender@example.com",
        connection_metadata={
            "inbox_access": True,
            "scopes": ["openid", "profile", "User.Read", "Mail.Send", MAIL_READ_SCOPE],
        },
    )
    session.add(connection)
    session.flush()
    account = SenderAccount(
        tenant_id=tenant.id,
        connection_id=connection.id,
        provider="MICROSOFT",
        email="sender@example.com",
        status="ACTIVE",
        health_status="HEALTHY",
    )
    session.add(account)
    session.flush()
    return engine, session, tenant, connection, account


def test_enable_reply_sync_requires_granted_mailbox_scope() -> None:
    engine, session, _tenant, connection, account = _inbox_bundle()
    try:
        connection.connection_metadata = {"inbox_access": True}  # consented, but not for Mail.Read
        session.commit()
        with pytest.raises(IntegrationValidationError):
            IntegrationService(session, connection.tenant_id).enable_reply_sync(account.id)
    finally:
        session.close()
        engine.dispose()


def test_enable_disable_reply_sync_round_trip() -> None:
    engine, session, _tenant, connection, account = _inbox_bundle()
    try:
        service = IntegrationService(session, connection.tenant_id)
        enabled = service.enable_reply_sync(account.id)
        assert enabled.reply_sync_enabled is True
        disabled = service.disable_reply_sync(account.id)
        assert disabled.reply_sync_enabled is False
    finally:
        session.close()
        engine.dispose()


def test_enable_reply_sync_refuses_unsupported_provider() -> None:
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    session = Session(engine)
    try:
        tenant = Tenant(name="Z", slug="z-tenant")
        session.add(tenant)
        session.flush()
        connection = SenderConnection(
            tenant_id=tenant.id, provider="ZOHO", connection_type="OAUTH", status="CONNECTED"
        )
        session.add(connection)
        session.flush()
        account = SenderAccount(
            tenant_id=tenant.id,
            connection_id=connection.id,
            provider="ZOHO",
            email="z@example.com",
            status="ACTIVE",
            health_status="HEALTHY",
        )
        session.add(account)
        session.commit()
        with pytest.raises(IntegrationValidationError):
            IntegrationService(session, tenant.id).enable_reply_sync(account.id)
    finally:
        session.close()
        engine.dispose()


def test_reply_sync_links_by_stored_message_id() -> None:
    engine, session, tenant, _connection, account = _inbox_bundle()
    try:
        campaign = Campaign(
            tenant_id=tenant.id,
            name="Link campaign",
            objective="Test",
            sender_id=account.id,
            status="APPROVED",
            schedule_config={"start_at": (datetime.now(UTC) - timedelta(days=1)).isoformat()},
        )
        session.add(campaign)
        session.flush()
        sender = EmailAccount(tenant_id=tenant.id, provider="MICROSOFT", email="sender@example.com", status="CONNECTED")
        session.add(sender)
        session.flush()
        contact = Contact(tenant_id=tenant.id, email="prospect@example.com", first_name="P")
        session.add(contact)
        session.flush()
        stored_id = f"{uuid4()}@example.com"
        original = Message(
            tenant_id=tenant.id,
            campaign_id=campaign.id,
            sender_id=sender.id,
            contact_id=contact.id,
            provider_message_id="accepted:1234567890abcdef",
            message_id=stored_id,
            subject="Campaign mail",
            status="SENT",
        )
        session.add(original)
        session.commit()

        raw: dict[str, object] = {
            "id": "reply-1",
            "thread_id": "t-1",
            "message_id": "<reply-1@example.com>",
            "in_reply_to": f"<{stored_id}>",
            "references": f"<{stored_id}>",
            "subject": "Re: Campaign mail",
            "from": "prospect@example.com",
            "received_at": datetime.now(UTC).isoformat(),
        }
        service = ReplySyncService(session, tenant.id)
        assert service._link(raw) is True
        session.expire_all()
        refreshed = session.get(Message, original.id)
        assert refreshed is not None
        assert [event.event_type for event in refreshed.events] == ["REPLY"]
    finally:
        session.close()
        engine.dispose()


def test_reply_sync_does_not_link_unrelated_reply() -> None:
    engine, session, tenant, _connection, _account = _inbox_bundle()
    try:
        sender = EmailAccount(tenant_id=tenant.id, provider="MICROSOFT", email="sender@example.com", status="CONNECTED")
        session.add(sender)
        session.flush()
        contact = Contact(tenant_id=tenant.id, email="prospect@example.com", first_name="P")
        session.add(contact)
        session.flush()
        service = ReplySyncService(session, tenant.id)
        assert service._link(
            {
                "in_reply_to": "<nonexistent@example.com>",
                "message_id": "reply-x",
            }
        ) is False
    finally:
        session.close()
        engine.dispose()