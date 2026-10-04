"""Phase 10C security + workflow tests: Microsoft 365 sender OAuth + Graph.

Covers the normalized ``MicrosoftEmailProvider`` (validate, discover, send,
error normalization, retry-after), the single-use state-bound consent round-trip,
and the System B Microsoft endpoints (/senders/microsoft/*) with RBAC + tenant
isolation + no-secret exposure. Microsoft Graph is mocked; no real credentials
are used in CI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, inspect
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import get_db
from app.email_providers import (
    EmailMessage,
    ProviderConnectionConfig,
    decrypt_credential_reference,
    encrypt_credential_reference,
    get_provider,
)
from app.email_providers.base import EmailProviderError, ProviderErrorCode
from app.main import app
from app.models import Base, Permission, Role, RolePermission, Tenant, User, UserRole
from app.security.passwords import hash_password
from app.security.tokens import TokenService
from app.services.oauth_state_store import OAuthStateStore

PASSWORD = "correct horse battery staple"
MS_CLIENT_ID = settings.microsoft_client_id or "microsoft-test-client-id"
MS_CLIENT_SECRET = settings.microsoft_client_secret or "microsoft-test-client-secret"
MS_EMAIL = "user@example.com"


@pytest.fixture(autouse=True)
def _microsoft_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = get_provider("MICROSOFT")

    def _fake_me(config) -> dict[str, object]:
        email = config.email or config.external_account_id or MS_EMAIL
        return {"id": email, "mail": email, "userPrincipalName": email, "displayName": "MS User"}

    def _fake_refresh(payload, *args, **kwargs):
        return "new-access-token", str(payload.get("refresh_token") or "refresh-token"), datetime.now(UTC) + timedelta(hours=1)

    def _fake_graph(method, path, config, *, json=None):
        return {}

    monkeypatch.setattr(provider, "_me", _fake_me, raising=False)
    monkeypatch.setattr(provider, "_refresh_access_token", _fake_refresh, raising=False)
    monkeypatch.setattr(provider, "_graph", _fake_graph, raising=False)


def _mint_token(user_id, tenant_id, roles) -> str:
    token, _ = TokenService(settings).create_access_token(user_id, tenant_id, roles)
    return token


@pytest.fixture()
def ms_client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ms_integrations.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    tenant_a = Tenant(name="Alpha", slug=f"a-{uuid4().hex[:8]}")
    tenant_b = Tenant(name="Beta", slug=f"b-{uuid4().hex[:8]}")
    session.add_all([tenant_a, tenant_b])
    session.flush()

    admin_role = Role(tenant_id=tenant_a.id, name="Admin")
    member_role = Role(tenant_id=tenant_a.id, name="Member")
    read_perm = Permission(key="integrations.read", description="Read integrations")
    b_admin_role = Role(tenant_id=tenant_b.id, name="Admin")
    session.add_all([admin_role, member_role, read_perm, b_admin_role])
    session.flush()

    admin = User(tenant_id=tenant_a.id, email="admin@example.com", password_hash=hash_password(PASSWORD), display_name="Admin")
    member = User(tenant_id=tenant_a.id, email="member@example.com", password_hash=hash_password(PASSWORD), display_name="Member")
    b_admin = User(tenant_id=tenant_b.id, email="badmin@example.com", password_hash=hash_password(PASSWORD), display_name="Beta Admin")
    session.add_all([admin, member, b_admin])
    session.flush()
    session.add_all(
        [
            UserRole(tenant_id=tenant_a.id, user_id=admin.id, role_id=admin_role.id),
            UserRole(tenant_id=tenant_a.id, user_id=member.id, role_id=member_role.id),
            RolePermission(role_id=member_role.id, permission_id=read_perm.id),
            UserRole(tenant_id=tenant_b.id, user_id=b_admin.id, role_id=b_admin_role.id),
        ]
    )
    session.commit()

    admin_token = _mint_token(admin.id, tenant_a.id, ["Admin"])
    member_token = _mint_token(member.id, tenant_a.id, ["Member"])
    b_admin_token = _mint_token(b_admin.id, tenant_b.id, ["Admin"])
    session.close()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app, follow_redirects=False)
    yield client, admin_token, member_token, b_admin_token, tenant_a.id, tenant_b.id
    app.dependency_overrides.clear()
    engine.dispose()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_microsoft(client, token, email=MS_EMAIL, external=MS_EMAIL) -> dict:
    return client.post(
        "/api/v1/integrations",
        json={"provider": "MICROSOFT", "connection_type": "OAUTH", "email": email, "external_account_id": external},
        headers=_headers(token),
    )


def _store_ms_tokens(client, token, connection_id) -> dict:
    return client.post(
        f"/api/v1/integrations/{connection_id}/credentials",
        json={
            "access_token": "ms-access-token",
            "refresh_token": "ms-refresh-token",
            "client_id": MS_CLIENT_ID,
            "scopes": ["openid", "profile", "email", "offline_access", "User.Read", "Mail.Send"],
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        headers=_headers(token),
    )


def _inject_state_store(monkeypatch: pytest.MonkeyPatch) -> OAuthStateStore:
    store = OAuthStateStore(memory_fallback=True)
    monkeypatch.setattr("app.services.microsoft_sender_oauth.build_state_store", lambda: store)
    return store


def _mock_ms_network(monkeypatch: pytest.MonkeyPatch, *, profile=None) -> None:
    import app.services.microsoft_sender_oauth as svc

    def _post(url, *args, **kwargs):
        res = Mock()
        res.ok = True
        res.status_code = 200
        res.json.return_value = {
            "access_token": "access",
            "refresh_token": "refresh",
            "scope": "openid profile email offline_access User.Read Mail.Send",
            "expires_in": 3600,
            "token_type": "Bearer",
        }
        return res

    def _get(url, *args, **kwargs):
        res = Mock()
        res.ok = True
        res.status_code = 200
        if profile is not None:
            res.json.return_value = profile
        else:
            res.json.return_value = {
                "id": MS_EMAIL,
                "mail": MS_EMAIL,
                "userPrincipalName": MS_EMAIL,
                "displayName": "MS User",
            }
        return res

    monkeypatch.setattr(svc.requests, "post", _post)
    monkeypatch.setattr(svc.requests, "get", _get)


def _ms_config() -> ProviderConnectionConfig:
    reference = encrypt_credential_reference(
        settings.encryption_key,
        {
            "access_token": "ms-token",
            "refresh_token": "ms-refresh",
            "client_id": MS_CLIENT_ID,
            "scopes": ["openid", "profile", "email", "offline_access", "User.Read", "Mail.Send"],
        },
    )
    return ProviderConnectionConfig(
        connection_type="OAUTH",
        external_account_id=MS_EMAIL,
        email=MS_EMAIL,
        metadata={"microsoft_id": MS_EMAIL},
        credential_reference=reference,
    )


# --------------------------------------------------------------------- #
# Provider unit tests
# --------------------------------------------------------------------- #
def test_microsoft_provider_capabilities_send_only() -> None:
    caps = get_provider("MICROSOFT").get_capabilities()
    assert caps.supports_oauth is True
    assert caps.supports_sender_discovery is True
    assert caps.supports_webhooks is False
    assert caps.supports_inbox_sync is True


def test_microsoft_validate_discover_send() -> None:
    provider = get_provider("MICROSOFT")
    assert provider.validate_connection(_ms_config()).valid is True

    discovered = provider.discover_senders(_ms_config())
    assert len(discovered.senders) == 1
    sender = discovered.senders[0]
    assert sender.email == MS_EMAIL
    assert sender.verified is True

    message = EmailMessage(from_email=MS_EMAIL, to=("recipient@example.com",), subject="Hello", text_body="Hi")
    message_id = provider.send_message(_ms_config(), message)
    assert message_id.startswith("accepted:")


def test_microsoft_rejects_invalid_recipient() -> None:
    provider = get_provider("MICROSOFT")
    message = EmailMessage(from_email=MS_EMAIL, to=("not-an-email",), subject="x")
    with pytest.raises(EmailProviderError) as exc_info:
        provider.send_message(_ms_config(), message)
    assert exc_info.value.code == ProviderErrorCode.INVALID_RECIPIENT


def test_microsoft_refresh_credentials_rotation() -> None:
    provider = get_provider("MICROSOFT")
    result = provider.refresh_credentials(_ms_config())
    payload = decrypt_credential_reference(settings.encryption_key, result.credential_reference)
    assert payload["access_token"] == "new-access-token"


def test_microsoft_error_normalization() -> None:
    provider = get_provider("MICROSOFT")

    class _Resp:
        def __init__(self, status, body=None, headers=None):
            self.status_code = status
            self._body = body
            self.headers = headers or {}

        def json(self):
            return self._body

    with pytest.raises(EmailProviderError) as e:
        provider._map_status(
            _Resp(429, {"error": {"code": "activityLimitReached", "message": "throttled"}}, {"Retry-After": "30"})
        )
    assert e.value.code == ProviderErrorCode.RATE_LIMITED
    assert e.value.retry_after == 30

    with pytest.raises(EmailProviderError) as e:
        provider._map_status(_Resp(403, {"error": {"code": "Authorization_RequestDenied", "message": "denied"}}))
    assert e.value.code == ProviderErrorCode.PERMISSION_DENIED

    with pytest.raises(EmailProviderError) as e:
        provider._map_status(_Resp(401, {"error": {"code": "InvalidAuthenticationToken", "message": "bad"}}))
    assert e.value.code == ProviderErrorCode.AUTH_FAILED

    with pytest.raises(EmailProviderError) as e:
        provider._map_status(_Resp(500, {"error": {"message": "server error"}}))
    assert e.value.code == ProviderErrorCode.PROVIDER_UNAVAILABLE


# --------------------------------------------------------------------- #
# OAuth authorize + consent URL
# --------------------------------------------------------------------- #
def test_microsoft_authorize_requires_permission(ms_client) -> None:
    client, _admin, member_token, *_ = ms_client
    response = client.post(
        "/api/v1/senders/microsoft/authorize",
        json={"connection_id": str(uuid4())},
        headers=_headers(member_token),
    )
    assert response.status_code == 403


def test_microsoft_authorize_missing_connection_404(ms_client) -> None:
    client, admin_token, *_ = ms_client
    assert client.post(
        "/api/v1/senders/microsoft/authorize",
        json={"connection_id": str(uuid4())},
        headers=_headers(admin_token),
    ).status_code == 404


def test_microsoft_authorize_returns_consent_url(ms_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    client, admin_token, *_ = ms_client
    connection_id = _create_microsoft(client, admin_token).json()["id"]
    response = client.post(
        "/api/v1/senders/microsoft/authorize",
        json={"connection_id": connection_id},
        headers=_headers(admin_token),
    )
    assert response.status_code == 200
    url = response.json()["authorization_url"]
    assert url.startswith("https://login.microsoftonline.com/common/oauth2/v2.0/authorize")
    assert "client_id=" + MS_CLIENT_ID in url
    assert "Mail.Send" in url
    assert "state=" in url


def test_microsoft_cross_tenant_authorize_404(ms_client) -> None:
    client, admin_token, _other, b_admin_token, *_ = ms_client
    connection_id = _create_microsoft(client, admin_token).json()["id"]
    assert client.post(
        "/api/v1/senders/microsoft/authorize",
        json={"connection_id": connection_id},
        headers=_headers(b_admin_token),
    ).status_code == 404


# --------------------------------------------------------------------- #
# OAuth callback (real complete flow, mocked network)
# --------------------------------------------------------------------- #
def test_microsoft_callback_rejects_bad_state(ms_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    client, *_ = ms_client
    response = client.get("/api/v1/senders/microsoft/oauth/callback", params={"code": "x", "state": "bogus"})
    assert response.status_code == 302
    assert "error=microsoft_oauth_error" in response.headers["location"]


def test_microsoft_callback_full_round_trip(ms_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    _mock_ms_network(monkeypatch)
    client, admin_token, *_ = ms_client
    connection = _create_microsoft(client, admin_token).json()
    connection_id = connection["id"]

    authorize = client.post(
        "/api/v1/senders/microsoft/authorize",
        json={"connection_id": connection_id},
        headers=_headers(admin_token),
    ).json()
    state = authorize["authorization_url"].split("state=", 1)[1].split("&", 1)[0]

    response = client.get("/api/v1/senders/microsoft/oauth/callback", params={"code": "auth-code", "state": state})
    assert response.status_code == 302
    assert "connected=1" in response.headers["location"]

    listed = client.get("/api/v1/integrations", headers=_headers(admin_token)).json()
    ms_conn = next(item for item in listed if item["provider"] == "MICROSOFT" and item["id"] == connection_id)
    assert ms_conn["status"] == "CONNECTED"
    assert ms_conn["email"] == MS_EMAIL
    assert ms_conn["external_account_id"] == MS_EMAIL
    assert ms_conn["credential_configured"] is True


def test_microsoft_state_is_single_use_replay_rejected(ms_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    _mock_ms_network(monkeypatch)
    client, admin_token, *_ = ms_client
    connection_id = _create_microsoft(client, admin_token).json()["id"]
    authorize = client.post(
        "/api/v1/senders/microsoft/authorize",
        json={"connection_id": connection_id},
        headers=_headers(admin_token),
    ).json()
    state = authorize["authorization_url"].split("state=", 1)[1].split("&", 1)[0]

    assert client.get("/api/v1/senders/microsoft/oauth/callback", params={"code": "c", "state": state}).status_code == 302
    # Replaying the same state must be rejected.
    replay = client.get("/api/v1/senders/microsoft/oauth/callback", params={"code": "c2", "state": state})
    assert replay.status_code == 302
    assert "error=microsoft_oauth_error" in replay.headers["location"]


def test_microsoft_scope_escalation_rejected(ms_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    import app.services.microsoft_sender_oauth as svc

    def _post(url, *args, **kwargs):
        res = Mock()
        res.ok = True
        res.status_code = 200
        res.json.return_value = {
            "access_token": "access",
            "refresh_token": "refresh",
            "scope": "openid profile email offline_access User.Read Mail.Send Mail.Read",  # escalated
            "expires_in": 3600,
        }
        return res

    monkeypatch.setattr(svc.requests, "post", _post)
    client, admin_token, *_ = ms_client
    connection_id = _create_microsoft(client, admin_token).json()["id"]
    authorize = client.post(
        "/api/v1/senders/microsoft/authorize",
        json={"connection_id": connection_id},
        headers=_headers(admin_token),
    ).json()
    state = authorize["authorization_url"].split("state=", 1)[1].split("&", 1)[0]
    response = client.get("/api/v1/senders/microsoft/oauth/callback", params={"code": "c", "state": state})
    assert response.status_code == 302
    assert "error=microsoft_oauth_error" in response.headers["location"]


def test_microsoft_token_exchange_failure_rejected(ms_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    import app.services.microsoft_sender_oauth as svc

    def _post(url, *args, **kwargs):
        res = Mock()
        res.ok = False
        res.status_code = 400
        return res

    monkeypatch.setattr(svc.requests, "post", _post)

    client, admin_token, *_ = ms_client
    # Create a connection whose state is bound, then exchange fails.
    connection = _create_microsoft(client, admin_token).json()
    connection_id = connection["id"]
    authorize = client.post(
        "/api/v1/senders/microsoft/authorize",
        json={"connection_id": connection_id},
        headers=_headers(admin_token),
    ).json()
    state = authorize["authorization_url"].split("state=", 1)[1].split("&", 1)[0]
    response = client.get("/api/v1/senders/microsoft/oauth/callback", params={"code": "c", "state": state})
    assert response.status_code == 302
    assert "error=microsoft_oauth_error" in response.headers["location"]


# --------------------------------------------------------------------- #
# Connection details + lifecycle endpoints
# --------------------------------------------------------------------- #
def test_microsoft_connection_details_never_returns_secrets(ms_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    _mock_ms_network(monkeypatch)
    client, admin_token, *_ = ms_client
    connection_id = _create_microsoft(client, admin_token).json()["id"]
    authorize = client.post(
        "/api/v1/senders/microsoft/authorize",
        json={"connection_id": connection_id},
        headers=_headers(admin_token),
    ).json()
    state = authorize["authorization_url"].split("state=", 1)[1].split("&", 1)[0]
    client.get("/api/v1/senders/microsoft/oauth/callback", params={"code": "c", "state": state})

    response = client.get(f"/api/v1/senders/microsoft/connections/{connection_id}", headers=_headers(admin_token))
    assert response.status_code == 200
    body = response.json()
    for forbidden in ("access_token", "refresh_token", "credential_reference", "client_secret"):
        assert forbidden not in body
        assert forbidden not in str(body)


def test_microsoft_validate_transitions(ms_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    _mock_ms_network(monkeypatch)
    client, admin_token, *_ = ms_client
    connection_id = _create_microsoft(client, admin_token).json()["id"]
    authorize = client.post(
        "/api/v1/senders/microsoft/authorize",
        json={"connection_id": connection_id},
        headers=_headers(admin_token),
    ).json()
    state = authorize["authorization_url"].split("state=", 1)[1].split("&", 1)[0]
    client.get("/api/v1/senders/microsoft/oauth/callback", params={"code": "c", "state": state})

    response = client.post(f"/api/v1/senders/microsoft/connections/{connection_id}/validate", headers=_headers(admin_token))
    assert response.status_code == 200
    assert response.json()["status"] == "CONNECTED"


def test_microsoft_disconnect(ms_client) -> None:
    client, admin_token, _member_token, b_admin_token, *_ = ms_client
    connection_id = _create_microsoft(client, admin_token).json()["id"]
    # Cross-tenant cannot disconnect.
    assert client.post(
        f"/api/v1/senders/microsoft/connections/{connection_id}/disconnect", headers=_headers(b_admin_token)
    ).status_code == 404
    response = client.post(f"/api/v1/senders/microsoft/connections/{connection_id}/disconnect", headers=_headers(admin_token))
    assert response.status_code == 200
    assert response.json()["status"] == "DISCONNECTED"


def test_microsoft_reconnect_returns_consent_url(ms_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    client, admin_token, _member_token, b_admin_token, *_ = ms_client
    connection_id = _create_microsoft(client, admin_token).json()["id"]
    assert client.post(
        f"/api/v1/senders/microsoft/connections/{connection_id}/reconnect", headers=_headers(b_admin_token)
    ).status_code == 404
    response = client.post(f"/api/v1/senders/microsoft/connections/{connection_id}/reconnect", headers=_headers(admin_token))
    assert response.status_code == 200
    assert response.json()["authorization_url"].startswith("https://login.microsoftonline.com/")


def test_microsoft_test_send_requires_connected(ms_client) -> None:
    client, admin_token, *_ = ms_client
    connection_id = _create_microsoft(client, admin_token).json()["id"]
    response = client.post(
        f"/api/v1/senders/microsoft/connections/{connection_id}/test-send",
        json={"recipient": "r@example.com"},
        headers=_headers(admin_token),
    )
    assert response.status_code == 400  # not yet connected


def test_microsoft_test_send_cross_tenant_404(ms_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    _mock_ms_network(monkeypatch)
    client, admin_token, _member_token, b_admin_token, *_ = ms_client
    connection_id = _create_microsoft(client, admin_token).json()["id"]
    authorize = client.post(
        "/api/v1/senders/microsoft/authorize",
        json={"connection_id": connection_id},
        headers=_headers(admin_token),
    ).json()
    state = authorize["authorization_url"].split("state=", 1)[1].split("&", 1)[0]
    client.get("/api/v1/senders/microsoft/oauth/callback", params={"code": "c", "state": state})
    assert client.post(
        f"/api/v1/senders/microsoft/connections/{connection_id}/test-send",
        json={"recipient": "r@example.com"},
        headers=_headers(b_admin_token),
    ).status_code == 404


# --------------------------------------------------------------------- #
# Schema / migration
# --------------------------------------------------------------------- #
def test_microsoft_unique_index_defined_in_schema(ms_client) -> None:
    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    indexes = {index["name"] for index in inspector.get_indexes("sender_connections")}
    assert "uq_sender_connections_microsoft_account" in indexes
    engine.dispose()
