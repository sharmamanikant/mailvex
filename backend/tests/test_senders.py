from __future__ import annotations

from typing import cast
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Tenant
from app.providers import (
    MockEmailProvider,
    ProviderMessage,
    ProviderProfile,
)
from app.schemas.senders import ProviderType, SenderCreate
from app.services.senders import SenderNotFoundError, SenderService


@pytest.fixture()
def sender_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'senders.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Sender Tenant", slug=f"sender-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        yield session, tenant.id
    engine.dispose()


def sender(provider: str = "GOOGLE") -> SenderCreate:
    return SenderCreate(email="owner@example.com", display_name="Owner", provider=cast(ProviderType, provider), encrypted_credential_ref="vault://credential/1", encryption_key_version="v1")


def test_provider_adapters_implement_mock_contract() -> None:
    profile = ProviderProfile(email="owner@example.com")
    for adapter in (MockEmailProvider("GOOGLE", profile), MockEmailProvider("MICROSOFT", profile), MockEmailProvider("SMTP", profile)):
        adapter.connect()
        assert adapter.authenticate()
        result = adapter.send(ProviderMessage("recipient@example.com", "Subject", "<p>Body</p>"))
        assert result.provider == adapter.provider
        assert adapter.health_check()


def test_sender_create_list_health_and_disconnect(sender_session) -> None:
    session, tenant_id = sender_session
    service = SenderService(session, tenant_id)
    account, authenticated = service.create(sender())
    assert authenticated is True
    assert account.status == "CONNECTED"
    assert account.credential is not None
    assert account.credential.encrypted_credential_ref == "vault://credential/1"
    assert len(service.list()) == 1
    checked, healthy = service.health_check(account.id)
    assert healthy is True
    assert checked.status == "CONNECTED"
    disconnected = service.disconnect(account.id)
    assert disconnected.status == "DISCONNECTED"
    assert disconnected.connection_status == "DISCONNECTED"


def test_sender_is_tenant_scoped(sender_session) -> None:
    session, tenant_id = sender_session
    account, _ = SenderService(session, tenant_id).create(sender())
    with pytest.raises(SenderNotFoundError):
        SenderService(session, uuid4()).get(account.id)
