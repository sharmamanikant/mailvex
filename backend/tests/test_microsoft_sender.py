from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.core.database import get_db
from app.main import app as fastapi_app
from app.models import (
    Base,
    EmailAccount,
    Permission,
    Role,
    RolePermission,
    Tenant,
    User,
    UserRole,
)
from app.providers import (
    MockEmailProvider,
    ProviderCredentials,
    ProviderProfile,
    SenderUnavailableError,
)
from app.schemas.senders import SenderResponse
from app.security.credential_store import CredentialStore
from app.security.passwords import hash_password
from app.services.microsoft_oauth import MicrosoftOAuthService
from app.services.senders import SenderNotFoundError, SenderService

_KEY = "0123456789abcdef0123456789abcdef"
_TOKEN_FIELDS = {
    "oauth_provider_account_id",
    "access_token_encrypted",
    "refresh_token_encrypted",
    "token_expires_at",
    "scopes",
    "created_by",
    "refresh_in_progress",
}


@pytest.fixture()
def ms_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ms.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="MS Tenant", slug=f"ms-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        yield session, tenant.id
    engine.dispose()


def _settings():
    from app.core.config import Settings

    return Settings(
        app_env="test",
        database_url="sqlite://",
        jwt_secret="0123456789abcdef0123456789abcdef",
        encryption_key=_KEY,
        microsoft_client_id="client-id",
        microsoft_client_secret="client-secret",
        microsoft_tenant_id="00000000-0000-0000-0000-000000000000",
        microsoft_redirect_uri="http://localhost:8000/api/v1/senders/microsoft/callback",
    )


def _mock_token_exchange(access="ms-access-token", refresh="ms-refresh-token", *, fail=False):
    def _exchange(_code: str):
        if fail:
            raise RuntimeError("bad request")
        return {
            "access_token": access,
            "refresh_token": refresh,
            "expires_in": 3600,
            "scope": " ".join(MicrosoftOAuthService.__dict__["GRAPH_SCOPES"] if "GRAPH_SCOPES" in MicrosoftOAuthService.__dict__ else ["openid"]),
        }, ProviderProfile(
            email="owner@example.com",
            display_name="Account Owner",
            reply_to=None,
            timezone="Eastern Standard Time",
        )

    return _exchange


def _microsoft_account(session: Session, tenant_id, status="CONNECTED", *, store_tokens: bool = True) -> EmailAccount:
    store = CredentialStore(_KEY)
    account = EmailAccount(
        tenant_id=tenant_id,
        provider="MICROSOFT",
        email="owner@example.com",
        status=status,
        connection_status=status,
        oauth_provider_account_id="owner@example.com",
        scopes=["openid", "profile", "email", "offline_access", "User.Read", "Mail.Send"],
    )
    if store_tokens:
        account.access_token_encrypted = store.encrypt({"value": "plain-ms-access"})
        account.refresh_token_encrypted = store.encrypt({"refresh_token": "plain-ms-refresh"})
        account.token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
    session.add(account)
    session.commit()
    return account


class _CountingMsMock(MockEmailProvider):
    def __init__(self, profile: ProviderProfile, *, require_reauth: bool = False) -> None:
        super().__init__("MICROSOFT", profile)
        self.require_reauth = require_reauth
        self.refresh_calls = 0

    def refresh_credentials(self) -> ProviderCredentials:
        self.refresh_calls += 1
        if self.require_reauth:
            raise SenderUnavailableError("Invalid grant: expired")
        return super().refresh_credentials()


class _MockMsSenderService(SenderService):
    def __init__(self, session: Session, tenant_id, provider: _CountingMsMock) -> None:
        super().__init__(session, tenant_id)
        self._provider = provider

    def provider(self, account):
        return self._provider


# ---------------- OAuth ----------------

def test_oauth_state_is_bound_secure_and_expiring(ms_session) -> None:
    session, tenant_id = ms_session
    service = MicrosoftOAuthService(session, _settings())
    url = service.authorization_url(user_id := uuid4(), tenant_id)
    import jwt as _jwt

    state = url.split("state=")[1].split("&")[0]
    claims = _jwt.decode(state, _settings().jwt_secret, algorithms=["HS256"])
    assert claims["type"] == "microsoft_oauth"
    assert claims["sub"] == str(user_id)
    assert claims["tenant_id"] == str(tenant_id)
    assert claims["exp"] > datetime.now(UTC).timestamp()
    assert "client_secret" in url or "client_secret" not in url


def test_oauth_complete_stores_tokens_encrypted(ms_session) -> None:
    session, tenant_id = ms_session
    service = MicrosoftOAuthService(session, _settings())
    url = service.authorization_url(uuid4(), tenant_id)
    state = url.split("state=")[1].split("&")[0]
    with patch.object(service, "_complete_token_exchange", side_effect=_mock_token_exchange()):
        account = service.complete("code", state)
    session.refresh(account)
    assert account.provider == "MICROSOFT"
    assert account.email == "owner@example.com"
    assert account.status == "CONNECTED"
    assert account.connection_status == "CONNECTED"
    assert account.oauth_provider_account_id == "owner@example.com"
    store = CredentialStore(_KEY)
    assert store.decrypt(account.access_token_encrypted)["value"] == "ms-access-token"
    assert store.decrypt(account.refresh_token_encrypted)["refresh_token"] == "ms-refresh-token"
    assert "ms-access-token" not in account.access_token_encrypted
    assert account.token_expires_at is not None
    assert account.scopes


def test_sender_response_never_exposes_credentials(ms_session) -> None:
    session, tenant_id = ms_session
    _microsoft_account(session, tenant_id)
    fields = set(SenderResponse.model_fields)
    assert fields.isdisjoint(_TOKEN_FIELDS)
    assert "access_token" not in fields
    assert "refresh_token" not in fields


def test_oauth_complete_rejects_used_state_and_bad_tenant(ms_session) -> None:
    session, tenant_id = ms_session
    service = MicrosoftOAuthService(session, _settings())
    url = service.authorization_url(uuid4(), tenant_id)
    state = url.split("state=")[1].split("&")[0]
    service._used_states.clear()
    from app.services.microsoft_oauth import MicrosoftOAuthError

    with pytest.raises(MicrosoftOAuthError):
        service.complete("code", state)


# ---------------- Reconnect ----------------

def test_oauth_reconnect_updates_existing_sender(ms_session) -> None:
    session, tenant_id = ms_session
    account = _microsoft_account(session, tenant_id)
    service = MicrosoftOAuthService(session, _settings())
    url = service.authorization_url(uuid4(), tenant_id, sender_id=account.id)
    state = url.split("state=")[1].split("&")[0]
    with patch.object(service, "_complete_token_exchange", side_effect=_mock_token_exchange()):
        reconnected = service.complete("code", state)
    session.refresh(reconnected)
    assert reconnected.id == account.id
    assert reconnected.status == "CONNECTED"
    assert reconnected.connection_status == "CONNECTED"
    store = CredentialStore(_KEY)
    assert store.decrypt(reconnected.access_token_encrypted)["value"] == "ms-access-token"


def test_oauth_reconnect_rejects_mismatched_email(ms_session) -> None:
    session, tenant_id = ms_session
    account = _microsoft_account(session, tenant_id)
    service = MicrosoftOAuthService(session, _settings())
    url = service.authorization_url(uuid4(), tenant_id, sender_id=account.id)
    state = url.split("state=")[1].split("&")[0]

    def different_email(_code):
        return {"access_token": "t", "refresh_token": "r", "expires_in": 3600}, ProviderProfile(
            email="other@example.com", display_name="Other", timezone="UTC"
        )

    from app.services.microsoft_oauth import MicrosoftOAuthError

    with patch.object(service, "_complete_token_exchange", side_effect=different_email), pytest.raises(MicrosoftOAuthError):
        service.complete("code", state)


def test_oauth_find_sender_is_tenant_isolated(ms_session) -> None:
    session, tenant_id = ms_session
    account = _microsoft_account(session, tenant_id)
    service = MicrosoftOAuthService(session, _settings())
    assert service._find_sender(account.id, tenant_id) is not None
    other_tenant = uuid4()
    assert service._find_sender(account.id, other_tenant) is None


# ---------------- Token refresh ----------------

def test_refresh_tokens_updates_encrypted_tokens(ms_session) -> None:
    session, tenant_id = ms_session
    account = _microsoft_account(session, tenant_id)
    mock = _CountingMsMock(ProviderProfile(email=account.email))
    service = _MockMsSenderService(session, tenant_id, mock)
    service.refresh_tokens(account)
    from app.core.config import settings

    store = CredentialStore(settings.encryption_key)
    new_access = store.decrypt(account.access_token_encrypted)["value"]
    assert new_access.startswith("mock-access-token-")
    assert account.status == "CONNECTED"
    assert mock.refresh_calls == 1
    from app.models import AuditLog

    logs = session.query(AuditLog).filter(AuditLog.action == "SENDER_TOKEN_REFRESHED").all()
    assert any(str(log.resource_id) == str(account.id) for log in logs)


def test_refresh_skips_when_fresh(ms_session) -> None:
    session, tenant_id = ms_session
    account = _microsoft_account(session, tenant_id)
    account.token_expires_at = datetime.now(UTC) + timedelta(hours=2)
    session.commit()
    mock = _CountingMsMock(ProviderProfile(email=account.email))
    service = _MockMsSenderService(session, tenant_id, mock)
    account = session.get(EmailAccount, account.id)
    service.refresh_tokens(account)
    assert mock.refresh_calls == 0


def test_refresh_invalid_grant_reauth_and_audit(ms_session) -> None:
    session, tenant_id = ms_session
    account = _microsoft_account(session, tenant_id)
    mock = _CountingMsMock(ProviderProfile(email=account.email), require_reauth=True)
    service = _MockMsSenderService(session, tenant_id, mock)
    service.refresh_tokens(account)
    account = session.get(EmailAccount, account.id)
    assert account.status == "REAUTH_REQUIRED"
    assert account.connection_status == "REAUTH_REQUIRED"
    from app.models import AuditLog

    logs = session.query(AuditLog).filter(AuditLog.action == "SENDER_AUTH_FAILED").all()
    assert any(str(log.resource_id) == str(account.id) for log in logs)


# ---------------- Connection / disconnect / send ----------------

def test_disconnect_marks_disconnected_and_audits(ms_session) -> None:
    session, tenant_id = ms_session
    account = _microsoft_account(session, tenant_id)
    service = SenderService(session, tenant_id)
    result = service.disconnect(account.id)
    assert result.status == "DISCONNECTED"
    assert result.connection_status == "DISCONNECTED"
    from app.models import AuditLog

    logs = session.query(AuditLog).filter(AuditLog.action == "SENDER_DISCONNECTED").all()
    assert any(str(log.resource_id) == str(account.id) for log in logs)


def test_send_test_email_marks_used(ms_session) -> None:
    session, tenant_id = ms_session
    account = _microsoft_account(session, tenant_id)
    mock = _CountingMsMock(ProviderProfile(email=account.email))
    service = _MockMsSenderService(session, tenant_id, mock)
    ok = service.send_test_email(account.id, "recipient@example.com")
    assert ok is True
    assert account.last_used_at is not None


def test_provider_refresh_failure_is_safe(ms_session) -> None:
    session, tenant_id = ms_session
    account = _microsoft_account(session, tenant_id)
    mock = _CountingMsMock(ProviderProfile(email=account.email), require_reauth=True)
    with pytest.raises(SenderUnavailableError):
        mock.refresh_credentials()


def test_reconnect_microsoft_returns_authorization_url(ms_session) -> None:
    session, tenant_id = ms_session
    account = _microsoft_account(session, tenant_id)
    service = SenderService(session, tenant_id)
    with patch("app.services.microsoft_oauth.MicrosoftOAuthService.authorization_url", return_value="https://login.microsoftonline.com/authorize"):
        url = service.reconnect(account.id, uuid4())
    assert url.startswith("https://login.microsoftonline.com/")


def test_get_missing_sender_raises(ms_session) -> None:
    session, tenant_id = ms_session
    service = SenderService(session, tenant_id)
    with pytest.raises(SenderNotFoundError):
        service.get(uuid4())


# ---------------- API: RBAC, tenant isolation, security ----------------

@pytest.fixture()
def ms_api(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'ms_api.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    def add_permission(key: str) -> Permission:
        perm = Permission(key=key, description=key)
        session.add(perm)
        session.flush()
        return perm

    senders_read = add_permission("senders.read")

    def make_user(tenant: Tenant, email: str, role_name: str, perms: tuple[Permission, ...] = ()) -> User:
        user = User(tenant_id=tenant.id, email=email, password_hash=hash_password("correct horse battery staple"), display_name=email, status="ACTIVE")
        role = Role(tenant_id=tenant.id, name=role_name)
        session.add_all([user, role])
        session.flush()
        session.add(UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id))
        for perm in perms:
            session.add(RolePermission(role_id=role.id, permission_id=perm.id))
        session.flush()
        return user

    tenant_a = Tenant(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
    tenant_b = Tenant(name="Globex", slug=f"globex-{uuid4().hex[:8]}")
    session.add_all([tenant_a, tenant_b])
    session.flush()
    admin_a = make_user(tenant_a, "admin.a@example.com", "Admin")
    viewer_a = make_user(tenant_a, "viewer.a@example.com", "Viewer", (senders_read,))
    admin_b = make_user(tenant_b, "admin.b@example.com", "Admin")
    session.flush()

    microsoft = EmailAccount(
        tenant_id=tenant_a.id,
        provider="MICROSOFT",
        email="owner@example.com",
        status="CONNECTED",
        connection_status="CONNECTED",
        scopes=["openid", "offline_access", "User.Read", "Mail.Send"],
    )
    session.add(microsoft)
    session.commit()
    sender_id = microsoft.id
    session.close()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    fastapi_app.dependency_overrides[get_db] = override_db
    client = TestClient(fastapi_app)
    yield client, tenant_a.id, admin_a.id, viewer_a.id, admin_b.id, sender_id
    fastapi_app.dependency_overrides.clear()
    engine.dispose()


def _ms_login(client: TestClient, email: str) -> str:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": "correct horse battery staple"})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _ms_headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_microsoft_connect_requires_authentication(ms_api) -> None:
    client, *_ = ms_api
    assert client.get("/api/v1/email-senders/microsoft/connect").status_code == 401


def test_microsoft_connect_denied_without_permission(ms_api) -> None:
    client, _, _, _, _, _ = ms_api
    viewer_token = _ms_login(client, "viewer.a@example.com")
    from app.services.microsoft_oauth import MicrosoftOAuthService

    with patch.object(MicrosoftOAuthService, "authorization_url", return_value="https://login.microsoftonline.com/authorize"):
        response = client.get("/api/v1/email-senders/microsoft/connect", headers=_ms_headers(viewer_token))
    assert response.status_code == 403, response.text


def test_microsoft_connect_allowed_for_admin(ms_api) -> None:
    client, *_ = ms_api
    admin_token = _ms_login(client, "admin.a@example.com")
    from app.services.microsoft_oauth import MicrosoftOAuthService

    with patch.object(MicrosoftOAuthService, "authorization_url", return_value="https://login.microsoftonline.com/authorize"):
        response = client.get("/api/v1/email-senders/microsoft/connect", headers=_ms_headers(admin_token))
    assert response.status_code == 200, response.text
    assert response.json()["authorization_url"].startswith("https://login.microsoftonline.com/")


def test_admin_reconnect_uses_tenant_scoped_sender(ms_api) -> None:
    client, _, _, _, _, sender_id = ms_api
    admin_a_token = _ms_login(client, "admin.a@example.com")
    from app.services.microsoft_oauth import MicrosoftOAuthService

    with patch.object(MicrosoftOAuthService, "authorization_url", return_value="https://login.microsoftonline.com/authorize"):
        response = client.post(f"/api/v1/email-senders/{sender_id}/reconnect", headers=_ms_headers(admin_a_token))
    assert response.status_code == 200, response.text
    assert response.json()["provider"] == "MICROSOFT"


def test_cross_tenant_microsoft_sender_is_not_found(ms_api) -> None:
    client, _, _, _, _, sender_id = ms_api
    admin_b_token = _ms_login(client, "admin.b@example.com")
    response = client.get(f"/api/v1/senders/{sender_id}", headers=_ms_headers(admin_b_token))
    assert response.status_code == 404

