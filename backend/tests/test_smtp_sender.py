from __future__ import annotations

import ipaddress
import smtplib
from unittest.mock import Mock
from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, EmailAccount, ProviderCredential, Tenant
from app.providers.base import ProviderMessage, ProviderProfile
from app.providers.smtp import SMTPPolicyError, SMTPProvider, SMTPProviderError
from app.schemas.senders import SenderResponse
from app.security.credential_store import CredentialStore
from app.security.smtp_policy import SMTPPolicyViolation, validate_destination
from app.services.senders import SenderNotFoundError, SenderService

_KEY = "0123456789abcdef0123456789abcdef"


def _resolver(*addresses: str):
    resol = [ipaddress.ip_address(a) for a in addresses]
    return lambda _host: resol


def _conn():
    return Mock()


# ---------------- Provider: transport & auth ----------------

def test_ssl_uses_smtp_ssl(monkeypatch) -> None:
    connection = _conn()
    ssl = Mock(return_value=connection)
    monkeypatch.setattr(smtplib, "SMTP_SSL", ssl)
    provider = SMTPProvider(ProviderProfile("from@example.com"), "smtp.example.com", 465, "SSL", validate=False)
    provider.connect()
    ssl.assert_called_once()
    assert provider.authenticate() is True
    provider.disconnect()


def test_starttls_and_login(monkeypatch) -> None:
    connection = _conn()
    monkeypatch.setattr(smtplib, "SMTP", Mock(return_value=connection))
    provider = SMTPProvider(ProviderProfile("from@example.com"), "smtp.example.com", 587, "STARTTLS", "user@example.com", "s3cret", validate=False)
    provider.connect()
    connection.ehlo.assert_called()
    connection.starttls.assert_called_once()
    connection.login.assert_called_once_with("user@example.com", "s3cret")
    provider.disconnect()


def test_invalid_credentials_raise(monkeypatch) -> None:
    connection = _conn()
    connection.login.side_effect = smtplib.SMTPAuthenticationError(535, b"authentication failed")
    monkeypatch.setattr(smtplib, "SMTP", Mock(return_value=connection))
    provider = SMTPProvider(ProviderProfile("from@example.com"), "smtp.example.com", 587, "STARTTLS", "user", "wrong", validate=False)
    with pytest.raises(SMTPProviderError, match="authentication failed") as exc:
        provider.connect()
    assert "wrong" not in str(exc.value)
    provider.disconnect()


def test_connection_refused_raises(monkeypatch) -> None:
    monkeypatch.setattr(smtplib, "SMTP", Mock(side_effect=OSError("connection refused")))
    provider = SMTPProvider(ProviderProfile("from@example.com"), "smtp.example.com", 587, "STARTTLS", validate=False)
    with pytest.raises(SMTPProviderError, match="connection"):
        provider.connect()
    provider.disconnect()


def test_timeout_raises(monkeypatch) -> None:
    monkeypatch.setattr(smtplib, "SMTP", Mock(side_effect=TimeoutError("timed out")))
    provider = SMTPProvider(ProviderProfile("from@example.com"), "smtp.example.com", 587, "STARTTLS", validate=False)
    with pytest.raises(SMTPProviderError):
        provider.connect()
    provider.disconnect()


def test_send_failure_raises(monkeypatch) -> None:
    connection = _conn()
    connection.send_message.side_effect = smtplib.SMTPRecipientsRefused({})
    monkeypatch.setattr(smtplib, "SMTP", Mock(return_value=connection))
    provider = SMTPProvider(ProviderProfile("from@example.com"), "smtp.example.com", 587, "STARTTLS", validate=False)
    provider.connect()
    with pytest.raises(SMTPProviderError, match="not accepted"):
        provider.send(ProviderMessage("to@example.com", "Hi", "<p>Hi</p>", "Hi"))
    provider.disconnect()


def test_disconnect_quits_and_clears(monkeypatch) -> None:
    connection = _conn()
    monkeypatch.setattr(smtplib, "SMTP", Mock(return_value=connection))
    provider = SMTPProvider(ProviderProfile("from@example.com"), "smtp.example.com", 587, "STARTTLS", validate=False)
    provider.connect()
    assert provider.authenticate() is True
    provider.disconnect()
    connection.quit.assert_called_once()
    assert provider.authenticate() is False


def test_password_never_in_error_or_repr(monkeypatch) -> None:
    connection = _conn()
    connection.login.side_effect = smtplib.SMTPAuthenticationError(535, b"bad")
    monkeypatch.setattr(smtplib, "SMTP", Mock(return_value=connection))
    provider = SMTPProvider(ProviderProfile("from@example.com"), "smtp.example.com", 587, "STARTTLS", "user", "super-secret-pw", validate=False)
    with pytest.raises(SMTPProviderError) as exc:
        provider.connect()
    assert "super-secret-pw" not in str(exc.value)
    assert "super-secret-pw" not in repr(provider)


# ---------------- Security policy: SSRF / ports / plaintext ----------------

def test_private_host_is_rejected() -> None:
    with pytest.raises(SMTPPolicyViolation, match="private or internal"):
        validate_destination("mail.internal", port=587, tls_mode="STARTTLS", username="u", password="p", resolver=_resolver("10.0.0.5"))


def test_loopback_is_rejected() -> None:
    with pytest.raises(SMTPPolicyViolation, match="private or internal"):
        validate_destination("localhost", port=587, tls_mode="STARTTLS", username="u", password="p", resolver=_resolver("127.0.0.1"))


def test_private_host_allowed_when_explicitly_permitted() -> None:
    validate_destination("smtp.internal", port=587, tls_mode="STARTTLS", username="u", password="p", allow_private_hosts=True, resolver=_resolver("10.1.2.3"))


def test_public_host_is_ok() -> None:
    validate_destination("smtp.example.com", port=587, tls_mode="STARTTLS", username="u", password="p", resolver=_resolver("93.184.216.34"))


def test_dns_rebinding_rejected_when_any_record_is_private() -> None:
    # One public and one private answer: if *any* points internally, reject.
    with pytest.raises(SMTPPolicyViolation):
        validate_destination("smtp.example.com", port=587, tls_mode="STARTTLS", username="u", password="p", resolver=_resolver("93.184.216.34", "169.254.169.254"))


def test_unsafe_port_is_rejected() -> None:
    with pytest.raises(SMTPPolicyViolation, match="port"):
        validate_destination("smtp.example.com", port=3306, tls_mode="STARTTLS", username="u", password="p", resolver=_resolver("93.184.216.34"))


def test_high_port_allowed() -> None:
    validate_destination("smtp.example.com", port=2525, tls_mode="STARTTLS", username="u", password="p", resolver=_resolver("93.184.216.34"))


def test_plaintext_auth_rejected_without_tls() -> None:
    with pytest.raises(SMTPPolicyViolation, match="TLS"):
        validate_destination("smtp.example.com", port=25, tls_mode="PLAIN", username="u", password="p", resolver=_resolver("93.184.216.34"))


def test_plaintext_auth_allowed_only_when_permitted() -> None:
    validate_destination("smtp.example.com", port=25, tls_mode="PLAIN", username="u", password="p", allow_plaintext_auth=True, resolver=_resolver("93.184.216.34"))


def test_provider_raises_policy_error_for_private_host() -> None:
    provider = SMTPProvider(ProviderProfile("from@example.com"), "smtp.internal", 587, "STARTTLS", "u", "p", resolver=_resolver("10.0.0.9"))
    with pytest.raises(SMTPPolicyError, match="private or internal"):
        provider.connect()
    provider.disconnect()


# ---------------- SenderService: test connection / test email / audit ----------------

@pytest.fixture()
def smtp_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'smtp.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="SMTP Tenant", slug=f"smtp-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        yield session, tenant.id
    engine.dispose()


def _populate_smtp(session: Session, tenant_id, email="sender@example.com") -> EmailAccount:
    store = CredentialStore(_KEY)
    account = EmailAccount(
        tenant_id=tenant_id,
        provider="SMTP",
        email=email,
        display_name="Sender",
        status="CONNECTED",
        connection_status="CONNECTED",
        smtp_host="smtp.example.com",
        smtp_port=587,
        smtp_tls_mode="STARTTLS",
        smtp_username="sender@example.com",
    )
    account.credential = ProviderCredential(tenant_id=tenant_id, email_account_id=account.id, encrypted_credential_ref="smtp-inline", encryption_key_version="v1", encrypted_credential_payload=store.encrypt({"password": "super-secret-pw"}))
    session.add(account)
    session.commit()
    return account


def test_connection_credentials_encrypted_at_rest(smtp_session) -> None:
    session, tenant_id = smtp_session
    account = _populate_smtp(session, tenant_id)
    session.refresh(account)
    assert account.credential is not None
    blob = account.credential.encrypted_credential_payload
    assert blob is not None
    assert "super-secret-pw" not in blob
    assert CredentialStore(_KEY).decrypt(blob)["password"] == "super-secret-pw"


def test_sender_response_never_exposes_password(smtp_session) -> None:
    session, tenant_id = smtp_session
    _populate_smtp(session, tenant_id)
    fields = set(SenderResponse.model_fields)
    assert "smtp_password" not in fields
    assert "password" not in fields
    assert "credential" not in fields


def test_test_connection_audits_without_sending(smtp_session, monkeypatch) -> None:
    session, tenant_id = smtp_session
    account = _populate_smtp(session, tenant_id)

    class FakeAdapter:
        def verify(self):
            return {"dns": True, "tcp": True, "tls": True, "auth": True}

        def disconnect(self):
            pass

    from app.services.senders import SenderService as S

    class _Svc(S):
        def provider(self, acct):
            return FakeAdapter()

    service = _Svc(session, tenant_id)
    result, ok = service.test_connection(account.id)
    assert ok is True
    assert result.status == "CONNECTED"
    from app.models import AuditLog

    logs = session.query(AuditLog).filter(AuditLog.action == "SENDER_TEST_CONNECTION").all()
    assert any(str(log.resource_id) == str(account.id) for log in logs)


def test_send_test_email_audits(smtp_session, monkeypatch) -> None:
    session, tenant_id = smtp_session
    account = _populate_smtp(session, tenant_id)
    sent = []

    class FakeAdapter:
        def connect(self):
            pass

        def disconnect(self):
            pass

        def send(self, msg):
            sent.append(msg)
            return Mock(id="smtp-1")

    from app.services.senders import SenderService as S

    class _Svc(S):
        def provider(self, acct):
            return FakeAdapter()

    service = _Svc(session, tenant_id)
    ok = service.send_test_email(account.id, "recipient@example.com")
    assert ok is True
    assert len(sent) == 1
    from app.models import AuditLog

    logs = session.query(AuditLog).filter(AuditLog.action == "SENDER_TEST_EMAIL").all()
    assert any(str(log.resource_id) == str(account.id) for log in logs)


def test_smtp_sender_is_tenant_isolated(smtp_session) -> None:
    session, tenant_id = smtp_session
    account = _populate_smtp(session, tenant_id)
    service_a = SenderService(session, tenant_id)
    assert service_a.get(account.id).id == account.id
    other_tenant = uuid4()
    other = SenderService(session, other_tenant)
    with pytest.raises(SenderNotFoundError):
        other.get(account.id)
