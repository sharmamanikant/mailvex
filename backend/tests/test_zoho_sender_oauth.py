"""Phase 10D security + workflow tests: Zoho Mail sender OAuth + REST.

Covers the normalized ``ZohoEmailProvider`` (validate, discover, send,
error normalization, retry-after, refresh rotation), the single-use
state-bound consent round-trip, and the System B Zoho endpoints
(/senders/zoho/*) with RBAC + tenant isolation + no-secret exposure. Zoho's
accounts token endpoint and userinfo are mocked; no real credentials are used
in CI.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import Mock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
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
ZOHO_CLIENT_ID = settings.zoho_client_id or "zoho-test-client-id"
ZOHO_EMAIL = "user@example.com"
ZOHO_ZUID = "zuid-12345"
ZOHO_DISPLAY = "Zoho User"
ZOHO_SCOPES_STR = "ZohoMail.messages.INSERT,ZohoMail.accounts.READ,Aao.profile.Read"


@pytest.fixture(autouse=True)
def _zoho_provider(monkeypatch: pytest.MonkeyPatch) -> None:
    provider = get_provider("ZOHO")

    def _fake_identity(config):
        email = config.email or config.external_account_id or ZOHO_EMAIL
        return ZOHO_ZUID, email.lower(), ZOHO_DISPLAY

    def _fake_refresh(payload, *args, **kwargs):
        return "new-zoho-access-token", str(payload.get("refresh_token") or "refresh-token"), datetime.now(UTC) + timedelta(hours=1)

    def _fake_zoho(method, path, config, *, json=None, params=None):
        return {"status": {"code": 2000, "description": "success"}, "data": []}

    def _fake_resolve(config):
        return "acc-1"

    monkeypatch.setattr(provider, "_identity", _fake_identity, raising=False)
    monkeypatch.setattr(provider, "_refresh_access_token", _fake_refresh, raising=False)
    monkeypatch.setattr(provider, "_zoho", _fake_zoho, raising=False)
    monkeypatch.setattr(provider, "_resolve_account_id", _fake_resolve, raising=False)


def _mint_token(user_id, tenant_id, roles) -> str:
    token, _ = TokenService(settings).create_access_token(user_id, tenant_id, roles)
    return token


@pytest.fixture()
def zoho_client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'zoho_integrations.db'}",
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


def _create_zoho(client, token, email=ZOHO_EMAIL) -> dict:
    return client.post(
        "/api/v1/integrations",
        json={"provider": "ZOHO", "connection_type": "OAUTH", "email": email},
        headers=_headers(token),
    )


def _inject_state_store(monkeypatch: pytest.MonkeyPatch) -> OAuthStateStore:
    store = OAuthStateStore(memory_fallback=True)
    monkeypatch.setattr("app.services.zoho_sender_oauth.build_state_store", lambda: store)
    return store


def _mock_zoho_network(monkeypatch: pytest.MonkeyPatch, *, profile=None) -> None:
    import app.services.zoho_sender_oauth as svc

    def _post(url, *args, **kwargs):
        res = Mock()
        res.ok = True
        res.status_code = 200
        res.json.return_value = {
            "access_token": "zoho-access",
            "refresh_token": "zoho-refresh",
            "scope": ZOHO_SCOPES_STR,
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
                "ZUID": ZOHO_ZUID,
                "Email": ZOHO_EMAIL,
                "Display_Name": ZOHO_DISPLAY,
            }
        return res

    monkeypatch.setattr(svc.requests, "post", _post)
    monkeypatch.setattr(svc.requests, "get", _get)


def _zoho_config() -> ProviderConnectionConfig:
    reference = encrypt_credential_reference(
        settings.encryption_key,
        {
            "access_token": "zoho-token",
            "refresh_token": "zoho-refresh",
            "client_id": ZOHO_CLIENT_ID,
            "scopes": ["ZohoMail.messages.INSERT", "ZohoMail.accounts.READ", "Aao.profile.Read"],
        },
    )
    return ProviderConnectionConfig(
        connection_type="OAUTH",
        external_account_id=ZOHO_ZUID,
        email=ZOHO_EMAIL,
        metadata={"zoho_id": ZOHO_ZUID},
        credential_reference=reference,
    )


# --------------------------------------------------------------------- #
# Provider unit tests
# --------------------------------------------------------------------- #
def test_zoho_provider_capabilities_send_only() -> None:
    caps = get_provider("ZOHO").get_capabilities()
    assert caps.supports_oauth is True
    assert caps.supports_sender_discovery is True
    assert caps.supports_api_key is False
    assert caps.supports_webhooks is False


def test_zoho_validate_discover_send() -> None:
    provider = get_provider("ZOHO")
    assert provider.validate_connection(_zoho_config()).valid is True

    discovered = provider.discover_senders(_zoho_config())
    assert len(discovered.senders) == 1
    sender = discovered.senders[0]
    assert sender.email == ZOHO_EMAIL
    assert sender.verified is True
    assert sender.external_sender_id == "acc-1"

    message = EmailMessage(from_email=ZOHO_EMAIL, to=("recipient@example.com",), subject="Hello", text_body="Hi")
    message_id = provider.send_message(_zoho_config(), message)
    assert message_id.startswith("accepted:")


def test_zoho_rejects_invalid_recipient() -> None:
    provider = get_provider("ZOHO")
    message = EmailMessage(from_email=ZOHO_EMAIL, to=("not-an-email",), subject="x")
    with pytest.raises(EmailProviderError) as exc_info:
        provider.send_message(_zoho_config(), message)
    assert exc_info.value.code == ProviderErrorCode.INVALID_RECIPIENT


def test_zoho_refresh_credentials_rotation() -> None:
    provider = get_provider("ZOHO")
    result = provider.refresh_credentials(_zoho_config())
    payload = decrypt_credential_reference(settings.encryption_key, result.credential_reference)
    assert payload["access_token"] == "new-zoho-access-token"


def test_zoho_error_normalization() -> None:
    import app.email_providers.zoho.provider as provider_module

    class _Resp:
        def __init__(self, status, headers=None):
            self.status_code = status
            self.headers = headers or {}
            self.content = b""
            self._body = {}

        def json(self):
            return self._body

    with pytest.raises(EmailProviderError) as e:
        raise provider_module._zoho_error(_Resp(429, {"Retry-After": "30"}), {"status": {"code": 2040, "description": "rate limit exceeded"}})
    assert e.value.code == ProviderErrorCode.RATE_LIMITED
    assert e.value.retry_after == 30

    with pytest.raises(EmailProviderError) as e:
        raise provider_module._zoho_error(_Resp(403), {"status": {"code": 2023, "description": "invalid token"}})
    assert e.value.code == ProviderErrorCode.AUTH_FAILED

    with pytest.raises(EmailProviderError) as e:
        raise provider_module._zoho_error(_Resp(403), {"status": {"code": 2048, "description": "permission denied"}})
    assert e.value.code == ProviderErrorCode.PERMISSION_DENIED


# --------------------------------------------------------------------- #
# OAuth authorize + consent URL
# --------------------------------------------------------------------- #
def test_zoho_authorize_requires_permission(zoho_client) -> None:
    client, _admin, member_token, *_ = zoho_client
    response = client.post(
        "/api/v1/senders/zoho/authorize",
        json={"connection_id": str(uuid4())},
        headers=_headers(member_token),
    )
    assert response.status_code == 403


def test_zoho_authorize_missing_connection_404(zoho_client) -> None:
    client, admin_token, *_ = zoho_client
    assert client.post(
        "/api/v1/senders/zoho/authorize",
        json={"connection_id": str(uuid4())},
        headers=_headers(admin_token),
    ).status_code == 404


def test_zoho_authorize_returns_consent_url(zoho_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    client, admin_token, *_ = zoho_client
    connection_id = _create_zoho(client, admin_token).json()["id"]
    response = client.post(
        "/api/v1/senders/zoho/authorize",
        json={"connection_id": connection_id},
        headers=_headers(admin_token),
    )
    assert response.status_code == 200
    url = response.json()["authorization_url"]
    assert url.startswith("https://accounts.zoho.com/oauth/v2/auth")
    assert "client_id=" + ZOHO_CLIENT_ID in url
    assert "ZohoMail.messages.INSERT" in url
    assert "access_type=offline" in url
    assert "state=" in url


def test_zoho_cross_tenant_authorize_404(zoho_client) -> None:
    client, admin_token, _other, b_admin_token, *_ = zoho_client
    connection_id = _create_zoho(client, admin_token).json()["id"]
    assert client.post(
        "/api/v1/senders/zoho/authorize",
        json={"connection_id": connection_id},
        headers=_headers(b_admin_token),
    ).status_code == 404


# --------------------------------------------------------------------- #
# OAuth callback (real complete flow, mocked network)
# --------------------------------------------------------------------- #
def test_zoho_callback_rejects_bad_state(zoho_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    client, *_ = zoho_client
    response = client.get("/api/v1/senders/zoho/oauth/callback", params={"code": "x", "state": "bogus"})
    assert response.status_code == 302
    assert "error=zoho_oauth_error" in response.headers["location"]


def test_zoho_callback_full_round_trip(zoho_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    _mock_zoho_network(monkeypatch)
    client, admin_token, *_ = zoho_client
    connection = _create_zoho(client, admin_token).json()
    connection_id = connection["id"]

    authorize = client.post(
        "/api/v1/senders/zoho/authorize",
        json={"connection_id": connection_id},
        headers=_headers(admin_token),
    ).json()
    state = authorize["authorization_url"].split("state=", 1)[1].split("&", 1)[0]

    response = client.get("/api/v1/senders/zoho/oauth/callback", params={"code": "auth-code", "state": state})
    assert response.status_code == 302
    assert "connected=1" in response.headers["location"]

    listed = client.get("/api/v1/integrations", headers=_headers(admin_token)).json()
    zoho_conn = next(item for item in listed if item["provider"] == "ZOHO" and item["id"] == connection_id)
    assert zoho_conn["status"] == "CONNECTED"
    assert zoho_conn["email"] == ZOHO_EMAIL
    assert zoho_conn["external_account_id"] == ZOHO_ZUID
    assert zoho_conn["credential_configured"] is True


def test_zoho_state_is_single_use_replay_rejected(zoho_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    _mock_zoho_network(monkeypatch)
    client, admin_token, *_ = zoho_client
    connection_id = _create_zoho(client, admin_token).json()["id"]
    authorize = client.post(
        "/api/v1/senders/zoho/authorize",
        json={"connection_id": connection_id},
        headers=_headers(admin_token),
    ).json()
    state = authorize["authorization_url"].split("state=", 1)[1].split("&", 1)[0]

    assert client.get("/api/v1/senders/zoho/oauth/callback", params={"code": "c", "state": state}).status_code == 302
    # Replaying the same state must be rejected.
    replay = client.get("/api/v1/senders/zoho/oauth/callback", params={"code": "c2", "state": state})
    assert replay.status_code == 302
    assert "error=zoho_oauth_error" in replay.headers["location"]


def test_zoho_scope_escalation_rejected(zoho_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    import app.services.zoho_sender_oauth as svc

    def _post(url, *args, **kwargs):
        res = Mock()
        res.ok = True
        res.status_code = 200
        res.json.return_value = {
            "access_token": "zoho-access",
            "refresh_token": "zoho-refresh",
            "scope": ZOHO_SCOPES_STR + ",ZohoMail.messages.READ",  # escalated
            "expires_in": 3600,
        }
        return res

    monkeypatch.setattr(svc.requests, "post", _post)
    client, admin_token, *_ = zoho_client
    connection_id = _create_zoho(client, admin_token).json()["id"]
    authorize = client.post(
        "/api/v1/senders/zoho/authorize",
        json={"connection_id": connection_id},
        headers=_headers(admin_token),
    ).json()
    state = authorize["authorization_url"].split("state=", 1)[1].split("&", 1)[0]
    response = client.get("/api/v1/senders/zoho/oauth/callback", params={"code": "c", "state": state})
    assert response.status_code == 302
    assert "error=zoho_oauth_error" in response.headers["location"]


def test_zoho_token_exchange_failure_rejected(zoho_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    import app.services.zoho_sender_oauth as svc

    def _post(url, *args, **kwargs):
        res = Mock()
        res.ok = False
        res.status_code = 400
        return res

    monkeypatch.setattr(svc.requests, "post", _post)

    client, admin_token, *_ = zoho_client
    connection = _create_zoho(client, admin_token).json()
    connection_id = connection["id"]
    authorize = client.post(
        "/api/v1/senders/zoho/authorize",
        json={"connection_id": connection_id},
        headers=_headers(admin_token),
    ).json()
    state = authorize["authorization_url"].split("state=", 1)[1].split("&", 1)[0]
    response = client.get("/api/v1/senders/zoho/oauth/callback", params={"code": "c", "state": state})
    assert response.status_code == 302
    assert "error=zoho_oauth_error" in response.headers["location"]


# --------------------------------------------------------------------- #
# Connection details + lifecycle endpoints
# --------------------------------------------------------------------- #
def test_zoho_connection_details_never_returns_secrets(zoho_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    _mock_zoho_network(monkeypatch)
    client, admin_token, *_ = zoho_client
    connection_id = _create_zoho(client, admin_token).json()["id"]
    authorize = client.post(
        "/api/v1/senders/zoho/authorize",
        json={"connection_id": connection_id},
        headers=_headers(admin_token),
    ).json()
    state = authorize["authorization_url"].split("state=", 1)[1].split("&", 1)[0]
    client.get("/api/v1/senders/zoho/oauth/callback", params={"code": "c", "state": state})

    response = client.get(f"/api/v1/senders/zoho/connections/{connection_id}", headers=_headers(admin_token))
    assert response.status_code == 200
    body = response.json()
    for forbidden in ("access_token", "refresh_token", "credential_reference", "client_secret"):
        assert forbidden not in body
        assert forbidden not in str(body)


def test_zoho_validate_transitions(zoho_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    _mock_zoho_network(monkeypatch)
    client, admin_token, *_ = zoho_client
    connection_id = _create_zoho(client, admin_token).json()["id"]
    authorize = client.post(
        "/api/v1/senders/zoho/authorize",
        json={"connection_id": connection_id},
        headers=_headers(admin_token),
    ).json()
    state = authorize["authorization_url"].split("state=", 1)[1].split("&", 1)[0]
    client.get("/api/v1/senders/zoho/oauth/callback", params={"code": "c", "state": state})

    response = client.post(f"/api/v1/senders/zoho/connections/{connection_id}/validate", headers=_headers(admin_token))
    assert response.status_code == 200
    assert response.json()["status"] == "CONNECTED"


def test_zoho_disconnect(zoho_client) -> None:
    client, admin_token, _member_token, b_admin_token, *_ = zoho_client
    connection_id = _create_zoho(client, admin_token).json()["id"]
    # Cross-tenant cannot disconnect.
    assert client.post(
        f"/api/v1/senders/zoho/connections/{connection_id}/disconnect", headers=_headers(b_admin_token)
    ).status_code == 404
    response = client.post(f"/api/v1/senders/zoho/connections/{connection_id}/disconnect", headers=_headers(admin_token))
    assert response.status_code == 200
    assert response.json()["status"] == "DISCONNECTED"


def test_zoho_reconnect_returns_consent_url(zoho_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    client, admin_token, _member_token, b_admin_token, *_ = zoho_client
    connection_id = _create_zoho(client, admin_token).json()["id"]
    assert client.post(
        f"/api/v1/senders/zoho/connections/{connection_id}/reconnect", headers=_headers(b_admin_token)
    ).status_code == 404
    response = client.post(f"/api/v1/senders/zoho/connections/{connection_id}/reconnect", headers=_headers(admin_token))
    assert response.status_code == 200
    assert response.json()["authorization_url"].startswith("https://accounts.zoho.com/oauth/v2/auth")


def test_zoho_test_send_requires_connected(zoho_client) -> None:
    client, admin_token, *_ = zoho_client
    connection_id = _create_zoho(client, admin_token).json()["id"]
    response = client.post(
        f"/api/v1/senders/zoho/connections/{connection_id}/test-send",
        json={"recipient": "r@example.com"},
        headers=_headers(admin_token),
    )
    assert response.status_code == 400  # not yet connected


def test_zoho_test_send_cross_tenant_404(zoho_client, monkeypatch) -> None:
    _inject_state_store(monkeypatch)
    _mock_zoho_network(monkeypatch)
    client, admin_token, _member_token, b_admin_token, *_ = zoho_client
    connection_id = _create_zoho(client, admin_token).json()["id"]
    authorize = client.post(
        "/api/v1/senders/zoho/authorize",
        json={"connection_id": connection_id},
        headers=_headers(admin_token),
    ).json()
    state = authorize["authorization_url"].split("state=", 1)[1].split("&", 1)[0]
    client.get("/api/v1/senders/zoho/oauth/callback", params={"code": "c", "state": state})
    assert client.post(
        f"/api/v1/senders/zoho/connections/{connection_id}/test-send",
        json={"recipient": "r@example.com"},
        headers=_headers(b_admin_token),
    ).status_code == 404