"""Spec-exact API aliases (Part 12) + bulk sender import lifecycle.

Covers the first-class ``/providers`` and ``/sender-connections`` surfaces and
the ``/integrations/senders/*`` bulk-import + enable/disable endpoints, all of
which delegate to the single ``IntegrationService`` implementation.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import get_db
from app.main import app
from app.models import Base, Role, Tenant, User, UserRole
from app.security.passwords import hash_password
from app.security.tokens import TokenService

PASSWORD = "correct horse battery staple"


class _FakeProfile:
    def __init__(self, email: str) -> None:
        self.email = email

    def execute(self) -> dict[str, object]:
        return {"emailAddress": self.email}


class _FakeUsers:
    def __init__(self, email: str) -> None:
        self.email = email

    def getProfile(self, userId: str) -> _FakeProfile:
        return _FakeProfile(self.email)


class _FakeMessages:
    def send(self, userId: str, body: dict[str, object]) -> _FakeSent:
        return _FakeSent()


class _FakeSent:
    def execute(self) -> dict[str, object]:
        return {"id": "msg-x"}


class _FakeGmailService:
    def __init__(self, email: str = "sender@example.com") -> None:
        self._users = _FakeUsers(email)
        self._messages = _FakeMessages()

    def users(self) -> _FakeUsers:
        return self._users

    def messages(self) -> _FakeMessages:
        return self._messages


@pytest.fixture(autouse=True)
def _patch_google_network(monkeypatch: pytest.MonkeyPatch) -> None:
    from app.email_providers import get_provider

    provider = get_provider("GOOGLE")

    def _fake_build(config) -> _FakeGmailService:
        email = (
            config.metadata.get("google_sub")
            or (config.email or "").lower()
            or (config.external_account_id or "sender@example.com").lower()
        )
        if "@" not in email:
            email = f"{email}@example.com"
        return _FakeGmailService(email)

    monkeypatch.setattr(provider, "_build_service", _fake_build, raising=False)


def _mint_token(user_id, tenant_id, roles) -> str:
    token, _ = TokenService(settings).create_access_token(user_id, tenant_id, roles)
    return token


@pytest.fixture()
def alias_client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'alias.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    tenant = Tenant(name="Alias Co", slug=f"ac-{uuid4().hex[:8]}")
    other_tenant = Tenant(name="Other Co", slug=f"oc-{uuid4().hex[:8]}")
    session.add_all([tenant, other_tenant])
    session.flush()

    admin_role = Role(tenant_id=tenant.id, name="Admin")
    session.add(admin_role)
    session.flush()
    admin = User(tenant_id=tenant.id, email="admin@alias.example", password_hash=hash_password(PASSWORD), display_name="Admin")
    other_user = User(tenant_id=other_tenant.id, email="other@alias.example", password_hash=hash_password(PASSWORD), display_name="Other")
    session.add_all([admin, other_user])
    session.flush()
    session.add_all(
        [
            UserRole(tenant_id=tenant.id, user_id=admin.id, role_id=admin_role.id),
            UserRole(tenant_id=other_tenant.id, user_id=other_user.id, role_id=admin_role.id),
        ]
    )
    session.commit()

    admin_token = _mint_token(admin.id, tenant.id, ["Admin"])
    other_token = _mint_token(other_user.id, other_tenant.id, ["Admin"])
    session.close()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    yield client, admin_token, other_token
    app.dependency_overrides.clear()
    engine.dispose()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def test_providers_alias_requires_auth(alias_client) -> None:
    client, *_ = alias_client
    assert client.get("/api/v1/providers").status_code == 401


def test_providers_alias_lists_capabilities(alias_client) -> None:
    client, admin_token, *_ = alias_client
    response = client.get("/api/v1/providers", headers=_headers(admin_token))
    assert response.status_code == 200
    providers = {item["provider_name"] for item in response.json()}
    assert providers == {"GOOGLE", "MICROSOFT", "ZOHO", "SENDGRID", "SMTP"}


def test_sender_connections_lifecycle_via_spec_paths(alias_client) -> None:
    client, admin_token, *_ = alias_client
    created = client.post(
        "/api/v1/sender-connections",
        json={"provider": "GOOGLE", "connection_type": "OAUTH", "external_account_id": "user/9"},
        headers=_headers(admin_token),
    )
    assert created.status_code == 201
    body = created.json()
    assert body["status"] == "CONNECTING"
    assert body["credential_configured"] is False
    connection_id = body["id"]

    listed = client.get("/api/v1/sender-connections", headers=_headers(admin_token))
    assert listed.status_code == 200
    assert connection_id in {item["id"] for item in listed.json()}

    fetched = client.get(f"/api/v1/sender-connections/{connection_id}", headers=_headers(admin_token))
    assert fetched.status_code == 200
    assert fetched.json()["id"] == connection_id


def test_sender_connections_validate_discover_disconnect(alias_client) -> None:
    client, admin_token, *_ = alias_client
    created = client.post(
        "/api/v1/sender-connections",
        json={"provider": "GOOGLE", "connection_type": "OAUTH", "external_account_id": "sender@example.com", "email": "sender@example.com", "metadata": {"google_sub": "sender@example.com", "simulated_discovery": ["a@example.com", "b@example.com"]}},
        headers=_headers(admin_token),
    ).json()
    connection_id = created["id"]
    client.post(
        f"/api/v1/integrations/{connection_id}/credentials",
        json={
            "access_token": "ya29.test-access-token",
            "refresh_token": "1//test-refresh-token",
            "client_id": "test-client",
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": ["openid", "https://www.googleapis.com/auth/gmail.send"],
        },
        headers=_headers(admin_token),
    )
    validated = client.post(f"/api/v1/sender-connections/{connection_id}/validate", headers=_headers(admin_token))
    assert validated.status_code == 200
    assert validated.json()["status"] == "CONNECTED"

    discovered = client.post(f"/api/v1/sender-connections/{connection_id}/discover", headers=_headers(admin_token))
    assert discovered.status_code == 200
    assert discovered.json()["discovered"] == 1
    assert {item["email"] for item in discovered.json()["senders"]} == {"sender@example.com"}

    disconnected = client.post(f"/api/v1/sender-connections/{connection_id}/disconnect", headers=_headers(admin_token))
    assert disconnected.json()["status"] == "DISCONNECTED"


def test_bulk_import_senders_with_duplicates(alias_client) -> None:
    client, admin_token, *_ = alias_client
    connection_id = client.post(
        "/api/v1/sender-connections",
        json={"provider": "GOOGLE", "connection_type": "OAUTH", "external_account_id": "user/9"},
        headers=_headers(admin_token),
    ).json()["id"]
    response = client.post(
        "/api/v1/integrations/senders/import",
        json={
            "connection_id": connection_id,
            "senders": [
                {"email": "one@example.com"},
                {"email": "two@example.com"},
                {"email": "one@example.com", "display_name": "Duplicate"},
            ],
        },
        headers=_headers(admin_token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["imported"] == 2
    assert body["skipped"] == 1
    assert any("already imported" in error for error in body["errors"])

    after = client.get(f"/api/v1/integrations/{connection_id}/senders", headers=_headers(admin_token)).json()
    assert {item["email"] for item in after} == {"one@example.com", "two@example.com"}


def test_disable_enable_sender_via_spec_endpoints(alias_client) -> None:
    client, admin_token, *_ = alias_client
    connection_id = client.post(
        "/api/v1/sender-connections",
        json={"provider": "GOOGLE", "connection_type": "OAUTH", "external_account_id": "user/9"},
        headers=_headers(admin_token),
    ).json()["id"]
    sender_id = client.post(
        f"/api/v1/integrations/{connection_id}/senders",
        json={"email": "flip@example.com"},
        headers=_headers(admin_token),
    ).json()["id"]
    disabled = client.post(f"/api/v1/integrations/senders/{sender_id}/disable", headers=_headers(admin_token))
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "DISABLED"
    enabled = client.post(f"/api/v1/integrations/senders/{sender_id}/enable", headers=_headers(admin_token))
    assert enabled.json()["status"] == "ACTIVE"


def test_alias_routes_are_tenant_scoped(alias_client) -> None:
    client, admin_token, other_token, *_ = alias_client
    connection_id = client.post(
        "/api/v1/sender-connections",
        json={"provider": "GOOGLE", "connection_type": "OAUTH", "external_account_id": "user/9"},
        headers=_headers(admin_token),
    ).json()["id"]
    assert client.get(f"/api/v1/sender-connections/{connection_id}", headers=_headers(other_token)).status_code == 404
    assert client.post(f"/api/v1/sender-connections/{connection_id}/validate", headers=_headers(other_token)).status_code == 404
    assert client.post(f"/api/v1/sender-connections/{connection_id}/disconnect", headers=_headers(other_token)).status_code == 404