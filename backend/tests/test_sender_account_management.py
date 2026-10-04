from __future__ import annotations

from datetime import UTC, datetime, timedelta
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    Campaign,
    Contact,
    EmailAccount,
    Tenant,
)
from app.providers import (
    MockEmailProvider,
    ProviderCredentials,
    ProviderProfile,
    SenderUnavailableError,
)
from app.schemas.senders import SenderResponse
from app.security.credential_store import CredentialStore
from app.services.compliance import ComplianceService
from app.services.senders import SenderService

_KEY = "0123456789abcdef0123456789abcdef"
_TOKENS = {
    "oauth_provider_account_id",
    "access_token_encrypted",
    "refresh_token_encrypted",
    "token_expires_at",
    "scopes",
    "created_by",
}


@pytest.fixture()
def account_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'accounts.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Account Tenant", slug=f"acct-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        yield session, tenant.id
    engine.dispose()


def _google_account(session: Session, tenant_id, status="CONNECTED", *, store_tokens: bool = True) -> EmailAccount:
    store = CredentialStore(_KEY)
    account = EmailAccount(
        tenant_id=tenant_id,
        provider="GOOGLE",
        email="owner@example.com",
        status=status,
        connection_status=status,
        scopes=["https://www.googleapis.com/auth/gmail.send"],
    )
    if store_tokens:
        account.access_token_encrypted = store.encrypt({"value": "plain-access-token"})
        account.refresh_token_encrypted = store.encrypt({"refresh_token": "plain-refresh-token"})
        account.token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    session.add(account)
    session.commit()
    return account


class _CountingMockProvider(MockEmailProvider):
    def __init__(self, profile: ProviderProfile, *, require_reauth: bool = False) -> None:
        super().__init__("GOOGLE", profile)
        self.require_reauth = require_reauth
        self.refresh_calls = 0

    def refresh_credentials(self) -> ProviderCredentials:
        self.refresh_calls += 1
        if self.require_reauth:
            raise SenderUnavailableError("Invalid grant: expired")
        return super().refresh_credentials()


class _MockSenderService(SenderService):
    def __init__(self, session: Session, tenant_id, provider: _CountingMockProvider) -> None:
        super().__init__(session, tenant_id)
        self._provider = provider

    def provider(self, account):
        return self._provider


def test_oauth_tokens_are_encrypted_not_plaintext(account_session) -> None:
    session, tenant_id = account_session
    store = CredentialStore(_KEY)
    account = _google_account(session, tenant_id)
    assert account.access_token_encrypted is not None
    assert "plain-access-token" not in account.access_token_encrypted
    assert "plain-refresh-token" not in account.refresh_token_encrypted
    decrypted = store.decrypt(account.access_token_encrypted)
    assert decrypted["value"] == "plain-access-token"
    refresh = store.decrypt(account.refresh_token_encrypted)
    assert refresh["refresh_token"] == "plain-refresh-token"


def test_sender_response_never_contains_credential_fields(account_session) -> None:
    session, tenant_id = account_session
    _google_account(session, tenant_id)
    fields = set(SenderResponse.model_fields)
    assert fields.isdisjoint(_TOKENS)
    assert "access_token" not in fields
    assert "refresh_token" not in fields


def test_refresh_tokens_updates_encrypted_tokens(account_session) -> None:
    session, tenant_id = account_session
    account = _google_account(session, tenant_id)
    mock = _CountingMockProvider(ProviderProfile(email=account.email))
    service = _MockSenderService(session, tenant_id, mock)
    service.refresh_tokens(account)
    from app.core.config import settings

    store = CredentialStore(settings.encryption_key)
    new_access = store.decrypt(account.access_token_encrypted)["value"]
    assert new_access.startswith("mock-access-token-")
    assert account.status == "CONNECTED"
    assert mock.refresh_calls == 1


def test_refresh_tokens_skips_when_still_fresh(account_session) -> None:
    session, tenant_id = account_session
    account = _google_account(session, tenant_id)
    account.token_expires_at = datetime.now(UTC) + timedelta(hours=2)
    session.commit()
    mock = _CountingMockProvider(ProviderProfile(email=account.email))
    service = _MockSenderService(session, tenant_id, mock)
    account = session.get(EmailAccount, account.id)
    service.refresh_tokens(account)
    assert mock.refresh_calls == 0


def test_refresh_tokens_invalid_grant_sets_reauth_and_audits(account_session) -> None:
    session, tenant_id = account_session
    account = _google_account(session, tenant_id)
    mock = _CountingMockProvider(ProviderProfile(email=account.email), require_reauth=True)
    service = _MockSenderService(session, tenant_id, mock)
    service.refresh_tokens(account)
    account = session.get(EmailAccount, account.id)
    assert account.status == "REAUTH_REQUIRED"
    assert account.connection_status == "REAUTH_REQUIRED"
    from app.models import AuditLog

    logs = session.query(AuditLog).filter(AuditLog.action == "SENDER_AUTH_FAILED").all()
    assert any(str(log.resource_id) == str(account.id) for log in logs)


def test_concurrent_refresh_runs_once(account_session) -> None:
    session, tenant_id = account_session
    account = _google_account(session, tenant_id)
    mock = _CountingMockProvider(ProviderProfile(email=account.email))
    service = _MockSenderService(session, tenant_id, mock)
    account = session.get(EmailAccount, account.id)
    service.refresh_tokens(account)
    service.refresh_tokens(account)
    assert mock.refresh_calls == 1


def test_disconnect_marks_disconnected(account_session) -> None:
    session, tenant_id = account_session
    account = _google_account(session, tenant_id)
    service = SenderService(session, tenant_id)
    result = service.disconnect(account.id)
    assert result.status == "DISCONNECTED"
    assert result.connection_status == "DISCONNECTED"


def test_compliance_blocks_disconnected_and_suspended(account_session) -> None:
    session, tenant_id = account_session
    for status in ("DISCONNECTED", "SUSPENDED", "REAUTH_REQUIRED"):
        account = _google_account(session, tenant_id, status=status)
        campaign = Campaign(
            tenant_id=tenant_id,
            name=f"C {status}",
            objective="Test",
            sender_id=account.id,
            status="APPROVED",
            schedule_config={},
        )
        contact = Contact(
            tenant_id=tenant_id,
            email="recipient@example.com",
            first_name="Recipient",
            validation_status="VALID",
        )
        session.add_all([campaign, contact])
        session.flush()
        result = ComplianceService(session, tenant_id).evaluate(campaign, account, contact)
        failure_names = {check.name for check in result.failures}
        assert "sender_health" in failure_names, status
        assert "provider_capacity" in failure_names, status
        session.query(Campaign).delete()
        session.query(Contact).delete()
        session.query(EmailAccount).delete()
        session.commit()


def test_reenabled_disconnected_sender_becomes_connected(account_session) -> None:
    session, tenant_id = account_session
    account = _google_account(session, tenant_id)
    service = SenderService(session, tenant_id)
    service.disconnect(account.id)
    account = session.get(EmailAccount, account.id)
    assert account.status == "DISCONNECTED"
    account, _ = service.enable(account.id)
    assert account.status == "CONNECTED"
    assert account.connection_status == "CONNECTED"
