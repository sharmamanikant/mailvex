"""Security + workflow tests for the email-provider foundation (System B).

Covers: provider registry/capabilities, connection lifecycle (create → store
encrypted credentials → validate), sender discovery/import, audit events, and
the security posture (cross-tenant isolation, RBAC, no credential exposure in
API responses or audit logs).
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
from app.models import Base, Permission, Role, RolePermission, Tenant, User, UserRole
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

    def messages(self) -> _FakeMessages:
        return _FakeMessages()


class _FakeMessages:
    def send(self, userId: str, body: dict[str, object]) -> _FakeSent:
        return _FakeSent()


class _FakeSent:
    def execute(self) -> dict[str, object]:
        return {"id": "msg-test-1"}


class _FakeGmailService:
    def __init__(self, email: str = "sender@example.com") -> None:
        self._email = email
        self._users = _FakeUsers(email)
        self._messages = _FakeMessages()

    def users(self) -> _FakeUsers:
        return self._users

    def messages(self) -> _FakeMessages:
        return self._messages


@pytest.fixture(autouse=True)
def _patch_provider_network(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace real provider network calls with fakes for this test module."""
    from app.email_providers import get_provider

    google = get_provider("GOOGLE")

    def _fake_build(config) -> _FakeGmailService:
        email = (
            config.metadata.get("google_sub")
            or (config.email or "").lower()
            or (config.external_account_id or "sender@example.com").lower()
        )
        if "@" not in email:
            email = f"{email}@example.com"
        return _FakeGmailService(email)

    monkeypatch.setattr(google, "_build_service", _fake_build, raising=False)

    import app.email_providers.sendgrid.provider as sendgrid_module
    import app.email_providers.smtp.provider as smtp_module

    monkeypatch.setattr(
        smtp_module,
        "validate_destination",
        lambda *args, **kwargs: None,
        raising=False,
    )
    monkeypatch.setattr(
        smtp_module.SMTPEmailProvider,
        "_connect",
        _fake_smtp_connect,
        raising=False,
    )
    monkeypatch.setattr(
        sendgrid_module.requests,
        "request",
        lambda *args, **kwargs: _FakeSendGridResponse(200),
        raising=False,
    )


class _FakeSMTPClient:
    def ehlo(self) -> tuple[int, bytes]:
        return 250, b"ok"

    def starttls(self, context: object | None = None) -> tuple[int, bytes]:
        return 220, b"ready"

    def login(self, *args: object, **kwargs: object) -> tuple[int, bytes]:
        return 235, b"auth ok"

    def send_message(self, message: object) -> dict[str, object]:
        return {}

    def quit(self) -> tuple[int, bytes]:
        return 221, b"bye"


def _fake_smtp_connect(self, host: str, port: int, security: str, timeout: float = 20.0) -> _FakeSMTPClient:
    return _FakeSMTPClient()


class _FakeSendGridResponse:
    def __init__(self, status_code: int) -> None:
        self.status_code = status_code
        self.headers: dict[str, str] = {}
        self.content = b"{}"
        self._json: object = {}

    def json(self) -> object:
        return self._json


def _mint_token(user_id, tenant_id, roles) -> str:
    token, _ = TokenService(settings).create_access_token(user_id, tenant_id, roles)
    return token


@pytest.fixture()
def integration_client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'integrations.db'}",
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
    client = TestClient(app)
    yield client, admin_token, member_token, b_admin_token, tenant_a.id, tenant_b.id
    app.dependency_overrides.clear()
    engine.dispose()


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _create_google(client, token, **overrides) -> dict:
    payload = {"provider": "GOOGLE", "connection_type": "OAUTH", "external_account_id": "sender@example.com", **overrides}
    return client.post("/api/v1/integrations", json=payload, headers=_headers(token))


def _store_api_key(client, token, connection_id, api_key="SG.secret-key-material") -> dict:
    return client.post(
        f"/api/v1/integrations/{connection_id}/credentials",
        json={"api_key": api_key},
        headers=_headers(token),
    )


def _store_google_tokens(client, token, connection_id) -> dict:
    return client.post(
        f"/api/v1/integrations/{connection_id}/credentials",
        json={
            "access_token": "ya29.test-access-token",
            "refresh_token": "1//test-refresh-token",
            "client_id": "test-client",
            "token_uri": "https://oauth2.googleapis.com/token",
            "scopes": ["openid", "https://www.googleapis.com/auth/gmail.send"],
        },
        headers=_headers(token),
    )


def _audit_items(client, token, action):
    response = client.get(
        "/api/v1/admin/audit-logs",
        params={"action": action, "page_size": 100},
        headers=_headers(token),
    )
    assert response.status_code == 200
    return response.json()["items"]


# --------------------------------------------------------------------- #
# Providers + capabilities
# --------------------------------------------------------------------- #
def test_providers_require_authentication(integration_client) -> None:
    client, *_ = integration_client
    assert client.get("/api/v1/integrations/providers").status_code == 401


def test_provider_capabilities_are_exposed(integration_client) -> None:
    client, admin_token, *_ = integration_client
    response = client.get("/api/v1/integrations/providers", headers=_headers(admin_token))
    assert response.status_code == 200
    providers = {item["provider_name"]: item for item in response.json()}
    assert set(providers) == {"GOOGLE", "MICROSOFT", "ZOHO", "SENDGRID", "SMTP"}
    assert providers["GOOGLE"]["supports_oauth"] is True
    assert providers["GOOGLE"]["connection_types"] == ["OAUTH"]
    assert providers["SENDGRID"]["supports_api_key"] is True
    assert providers["SMTP"]["supports_smtp"] is True
    assert providers["MICROSOFT"]["supports_oauth"] is True
    assert providers["MICROSOFT"]["supports_inbox_sync"] is True


# --------------------------------------------------------------------- #
# Connection lifecycle
# --------------------------------------------------------------------- #
def test_create_connection_initially_connecting_no_credentials(integration_client) -> None:
    client, admin_token, *_ = integration_client
    response = _create_google(client, admin_token)
    assert response.status_code == 201
    body = response.json()
    assert body["status"] == "CONNECTING"
    assert body["credential_configured"] is False
    assert body["provider"] == "GOOGLE"
    assert "credential_reference" not in body


def test_create_unknown_provider_rejected(integration_client) -> None:
    client, admin_token, *_ = integration_client
    response = client.post(
        "/api/v1/integrations",
        json={"provider": "YAHOO", "connection_type": "OAUTH"},
        headers=_headers(admin_token),
    )
    assert response.status_code == 400


def test_create_unsupported_connection_type_rejected(integration_client) -> None:
    client, admin_token, *_ = integration_client
    response = _create_google(client, admin_token, connection_type="API_KEY")
    assert response.status_code == 400


def test_credentials_are_encrypted_and_never_returned(integration_client) -> None:
    client, admin_token, *_ = integration_client
    created = _create_google(client, admin_token).json()
    connection_id = created["id"]
    secret = "SG-super-secret-api-key"
    store = _store_api_key(client, admin_token, connection_id, api_key=secret)
    assert store.status_code == 200
    assert store.json()["configured"] is True
    assert store.json()["credential_version"] == "v1"
    assert secret not in str(store.json())

    listed = client.get(f"/api/v1/integrations/{connection_id}", headers=_headers(admin_token)).json()
    assert listed["credential_configured"] is True
    for forbidden in ("credential_reference", "api_key", "smtp_password", "client_secret", "access_token", "refresh_token"):
        assert forbidden not in listed
    assert secret not in str(listed)


def test_validate_without_credentials_marks_failed(integration_client) -> None:
    client, admin_token, *_ = integration_client
    connection_id = _create_google(client, admin_token).json()["id"]
    response = client.post(f"/api/v1/integrations/{connection_id}/validate", headers=_headers(admin_token))
    assert response.status_code == 200
    assert response.json()["status"] == "REAUTH_REQUIRED"
    reauth_events = _audit_items(client, admin_token, "GOOGLE_CONNECTION_REAUTH_REQUIRED")
    assert any(item["entity_id"] == connection_id for item in reauth_events)


def test_validate_with_credentials_connects(integration_client) -> None:
    client, admin_token, *_ = integration_client
    connection_id = _create_google(client, admin_token).json()["id"]
    _store_google_tokens(client, admin_token, connection_id)
    response = client.post(f"/api/v1/integrations/{connection_id}/validate", headers=_headers(admin_token))
    assert response.status_code == 200
    assert response.json()["status"] == "CONNECTED"
    valid_events = _audit_items(client, admin_token, "GOOGLE_CONNECTION_VALIDATED")
    assert any(item["entity_id"] == connection_id for item in valid_events)


def test_sendgrid_api_key_flow(integration_client) -> None:
    client, admin_token, *_ = integration_client
    created = client.post(
        "/api/v1/integrations",
        json={"provider": "SENDGRID", "connection_type": "API_KEY"},
        headers=_headers(admin_token),
    ).json()
    response = client.post(
        f"/api/v1/integrations/{created['id']}/credentials",
        json={"api_key": "SG.my-api-key"},
        headers=_headers(admin_token),
    )
    assert response.status_code == 200
    validated = client.post(f"/api/v1/integrations/{created['id']}/validate", headers=_headers(admin_token)).json()
    assert validated["status"] == "CONNECTED"


def test_smtp_connection_requires_host_port_and_credentials(integration_client) -> None:
    client, admin_token, *_ = integration_client
    created = client.post(
        "/api/v1/integrations",
        json={"provider": "SMTP", "connection_type": "SMTP", "email": "sender@example.com"},
        headers=_headers(admin_token),
    ).json()
    partial = client.post(f"/api/v1/integrations/{created['id']}/validate", headers=_headers(admin_token)).json()
    assert partial["status"] == "FAILED"
    client.post(
        f"/api/v1/integrations/{created['id']}/credentials",
        json={"smtp_password": "smtp-secret", "smtp_host": "smtp.example.com", "smtp_port": 587},
        headers=_headers(admin_token),
    )
    validated = client.post(f"/api/v1/integrations/{created['id']}/validate", headers=_headers(admin_token)).json()
    assert validated["status"] == "CONNECTED"
    print("DEBUG_SMTP_VALIDATED", validated)


def test_validate_missing_connection_404(integration_client) -> None:
    client, admin_token, *_ = integration_client
    response = client.post("/api/v1/integrations/00000000-0000-0000-0000-000000000000/validate", headers=_headers(admin_token))
    assert response.status_code == 404


# --------------------------------------------------------------------- #
# Discovery, accounts, audit
# --------------------------------------------------------------------- #
def test_discovery_records_events(integration_client) -> None:
    client, admin_token, *_ = integration_client
    created = _create_google(client, admin_token).json()
    connection_id = created["id"]
    _store_google_tokens(client, admin_token, connection_id)
    response = client.post(f"/api/v1/integrations/{connection_id}/discover", headers=_headers(admin_token))
    assert response.status_code == 200
    body = response.json()
    assert body["supports_discovery"] is True
    assert body["discovered"] == 1
    assert {item["email"] for item in body["senders"]} == {"sender@example.com"}
    started = _audit_items(client, admin_token, "SENDER_DISCOVERY_STARTED")
    done = _audit_items(client, admin_token, "SENDER_DISCOVERED")
    assert any(item["entity_id"] == connection_id for item in started)
    assert any(item["entity_id"] == connection_id and item["metadata"].get("discovered") == 1 for item in done)


def test_import_sender_and_duplicate_conflict(integration_client) -> None:
    client, admin_token, *_ = integration_client
    connection_id = _create_google(client, admin_token).json()["id"]
    response = client.post(
        f"/api/v1/integrations/{connection_id}/senders",
        json={"email": "inbox@example.com", "display_name": "Inbox", "external_sender_id": "send-era"},
        headers=_headers(admin_token),
    )
    assert response.status_code == 201
    assert response.json()["status"] == "ACTIVE"
    duplicate = client.post(
        f"/api/v1/integrations/{connection_id}/senders",
        json={"email": "inbox@example.com"},
        headers=_headers(admin_token),
    )
    assert duplicate.status_code == 409
    imported = _audit_items(client, admin_token, "SENDER_IMPORTED")
    assert any(item["metadata"].get("email") == "inbox@example.com" for item in imported)


def test_disable_and_enable_sender_audited(integration_client) -> None:
    client, admin_token, *_ = integration_client
    connection_id = _create_google(client, admin_token).json()["id"]
    sender_id = client.post(
        f"/api/v1/integrations/{connection_id}/senders",
        json={"email": "flip@example.com"},
        headers=_headers(admin_token),
    ).json()["id"]
    disabled = client.patch(
        f"/api/v1/integrations/senders/{sender_id}",
        json={"status": "DISABLED"},
        headers=_headers(admin_token),
    )
    assert disabled.status_code == 200
    assert disabled.json()["status"] == "DISABLED"
    enabled = client.patch(
        f"/api/v1/integrations/senders/{sender_id}",
        json={"status": "ACTIVE"},
        headers=_headers(admin_token),
    )
    assert enabled.json()["status"] == "ACTIVE"
    assert any(item["entity_id"] == sender_id for item in _audit_items(client, admin_token, "SENDER_DISABLED"))
    assert any(item["entity_id"] == sender_id for item in _audit_items(client, admin_token, "SENDER_ENABLED"))


def test_refresh_credentials_rotates_version(integration_client) -> None:
    client, admin_token, *_ = integration_client
    connection_id = _create_google(client, admin_token).json()["id"]
    _store_api_key(client, admin_token, connection_id)
    refreshed = client.post(f"/api/v1/integrations/{connection_id}/refresh-credentials", headers=_headers(admin_token))
    assert refreshed.status_code == 200
    assert refreshed.json()["credential_version"] == "v2"
    assert refreshed.json()["configured"] is True
    rotated = _audit_items(client, admin_token, "SENDER_CREDENTIAL_ROTATED")
    assert any(item["entity_id"] == connection_id and item["metadata"].get("version") == "v2" for item in rotated)


def _register_sender(client, token, connection_id, email="toggle@example.com") -> dict:
    response = client.post(
        f"/api/v1/integrations/{connection_id}/senders",
        json={"email": email},
        headers=_headers(token),
    )
    assert response.status_code == 201
    return response.json()


def test_campaign_toggles_are_opt_in_and_audited(integration_client) -> None:
    client, admin_token, member_token, *_ = integration_client
    connection_id = _create_google(client, admin_token).json()["id"]
    sender = _register_sender(client, admin_token, connection_id)

    # New senders never auto-enable campaign traffic.
    listed = client.get(f"/api/v1/integrations/{connection_id}/senders", headers=_headers(admin_token)).json()
    assert listed[0]["campaign_enabled"] is False
    assert listed[0]["warmup_enabled"] is False

    # Member lacks the connect permission.
    assert client.post(
        f"/api/v1/integrations/senders/{sender['id']}/enable-campaign", headers=_headers(member_token)
    ).status_code == 403

    enabled = client.post(
        f"/api/v1/integrations/senders/{sender['id']}/enable-campaign", headers=_headers(admin_token)
    ).json()
    assert enabled["campaign_enabled"] is True
    disabled = client.post(
        f"/api/v1/integrations/senders/{sender['id']}/disable-campaign", headers=_headers(admin_token)
    ).json()
    assert disabled["campaign_enabled"] is False
    assert any(item["entity_id"] == sender["id"] for item in _audit_items(client, admin_token, "SENDER_CAMPAIGN_ENABLED"))
    assert any(item["entity_id"] == sender["id"] for item in _audit_items(client, admin_token, "SENDER_CAMPAIGN_DISABLED"))


def test_warmup_toggles_and_settings_round_trip(integration_client) -> None:
    client, admin_token, *_ = integration_client
    connection_id = _create_google(client, admin_token).json()["id"]
    sender = _register_sender(client, admin_token, connection_id)
    sender_id = sender["id"]

    enabled = client.post(f"/api/v1/integrations/senders/{sender_id}/enable-warmup", headers=_headers(admin_token)).json()
    assert enabled["warmup_enabled"] is True

    settings = client.get(f"/api/v1/integrations/senders/{sender_id}/warmup", headers=_headers(admin_token))
    assert settings.status_code == 200
    body = settings.json()
    assert body["enabled"] is True
    assert body["daily_limit"] == 20
    assert body["weekdays"] != []

    updated = client.put(
        f"/api/v1/integrations/senders/{sender_id}/warmup",
        json={
            "daily_limit": 15,
            "reply_rate_target": 0.7,
            "start_time": "09:00",
            "end_time": "17:00",
            "weekdays": ["MON", "WED", "FRI"],
            "minimum_delay": 30,
            "maximum_delay": 120,
            "target_provider_distribution": {"GOOGLE": 1.0},
        },
        headers=_headers(admin_token),
    )
    assert updated.status_code == 200
    body = updated.json()
    assert body["daily_limit"] == 15
    assert body["reply_rate_target"] == 0.7
    assert body["weekdays"] == ["MON", "WED", "FRI"]
    assert body["minimum_delay"] == 30
    assert body["maximum_delay"] == 120

    disabled = client.post(f"/api/v1/integrations/senders/{sender_id}/disable-warmup", headers=_headers(admin_token)).json()
    assert disabled["warmup_enabled"] is False
    assert client.get(f"/api/v1/integrations/senders/{sender_id}/warmup", headers=_headers(admin_token)).json()["enabled"] is False
    assert any(item["entity_id"] == sender_id for item in _audit_items(client, admin_token, "WARMUP_SETTINGS_UPDATED"))


def test_warmup_settings_validate_delays_and_weekdays(integration_client) -> None:
    client, admin_token, *_ = integration_client
    connection_id = _create_google(client, admin_token).json()["id"]
    sender_id = _register_sender(client, admin_token, connection_id)["id"]

    bad_delays = client.put(
        f"/api/v1/integrations/senders/{sender_id}/warmup",
        json={"minimum_delay": 600, "maximum_delay": 60},
        headers=_headers(admin_token),
    )
    assert bad_delays.status_code == 400

    bad_weekdays = client.put(
        f"/api/v1/integrations/senders/{sender_id}/warmup",
        json={"weekdays": ["MON", "FUNDAY"]},
        headers=_headers(admin_token),
    )
    assert bad_weekdays.status_code == 400

    bad_time = client.put(
        f"/api/v1/integrations/senders/{sender_id}/warmup",
        json={"start_time": "25:99"},
        headers=_headers(admin_token),
    )
    assert bad_time.status_code == 422


def test_list_all_senders_is_tenant_scoped(integration_client) -> None:
    client, admin_token, *_ = integration_client
    connection_a = _create_google(client, admin_token, email="a@example.com").json()["id"]
    _register_sender(client, admin_token, connection_a, email="tenant@example.com")

    # Cross-tenant cannot see or toggle another tenant's senders.
    client_b, _admin_b, _member_b, b_admin_token, *_ = integration_client  # same DB, tenant b
    listed_b = client_b.get("/api/v1/integrations/senders", headers=_headers(b_admin_token))
    assert listed_b.status_code == 200
    assert listed_b.json() == []

    listed_a = client.get("/api/v1/integrations/senders", headers=_headers(admin_token)).json()
    assert [item["email"] for item in listed_a] == ["tenant@example.com"]


# --------------------------------------------------------------------- #
# Security posture
# --------------------------------------------------------------------- #
def test_cross_tenant_connections_are_invisible(integration_client) -> None:
    client, admin_token, _member_token, other_token, *_ = integration_client
    connection_id = _create_google(client, admin_token).json()["id"]
    assert client.get(f"/api/v1/integrations/{connection_id}", headers=_headers(other_token)).status_code == 404
    assert client.post(f"/api/v1/integrations/{connection_id}/validate", headers=_headers(other_token)).status_code == 404
    assert client.post(f"/api/v1/integrations/{connection_id}/discover", headers=_headers(other_token)).status_code == 404
    assert client.get(f"/api/v1/integrations/{connection_id}/senders", headers=_headers(other_token)).status_code == 404
    assert client.delete(f"/api/v1/integrations/{connection_id}", headers=_headers(other_token)).status_code == 404
    other_list = client.get("/api/v1/integrations", headers=_headers(other_token)).json()
    assert other_list == []  # no cross-tenant rows leak into tenant B's list


def test_cross_tenant_sender_account_is_invisible(integration_client) -> None:
    client, admin_token, _member_token, other_token, *_ = integration_client
    connection_id = _create_google(client, admin_token).json()["id"]
    sender_id = client.post(
        f"/api/v1/integrations/{connection_id}/senders",
        json={"email": "private@example.com"},
        headers=_headers(admin_token),
    ).json()["id"]
    patched = client.patch(
        f"/api/v1/integrations/senders/{sender_id}",
        json={"status": "DISABLED"},
        headers=_headers(other_token),
    )
    assert patched.status_code == 404


def test_rbac_enforces_connect_permission(integration_client) -> None:
    client, _admin_token, member_token, *_ = integration_client
    assert client.get("/api/v1/integrations/providers", headers=_headers(member_token)).status_code == 200
    response = client.post(
        "/api/v1/integrations",
        json={"provider": "GOOGLE", "connection_type": "OAUTH", "external_account_id": "x"},
        headers=_headers(member_token),
    )
    assert response.status_code == 403
    assert client.post("/api/v1/integrations", json={"provider": "GOOGLE", "connection_type": "OAUTH"}, headers=_headers(member_token)).status_code == 403


def test_audit_logs_never_contain_credentials(integration_client) -> None:
    client, admin_token, *_ = integration_client
    secret = "ULTRA-SECRET-API-KEY-42"
    created = _create_google(client, admin_token).json()
    _store_api_key(client, admin_token, created["id"], api_key=secret)
    client.post(f"/api/v1/integrations/{created['id']}/validate", headers=_headers(admin_token))
    all_logs = client.get("/api/v1/admin/audit-logs", params={"page_size": 100}, headers=_headers(admin_token)).json()["items"]
    serialized = str(all_logs)
    assert secret not in serialized
    assert "credential_reference" not in serialized


def test_model_has_connection_email_and_metadata(integration_client) -> None:
    client, admin_token, *_ = integration_client
    response = _create_google(client, admin_token, email="envelope@example.com")
    assert response.status_code == 201
    assert response.json()["email"] == "envelope@example.com"


def test_generic_sender_connection_table_constraints() -> None:
    from sqlalchemy import inspect

    engine = create_engine("sqlite://", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    inspector = inspect(engine)
    assert "sender_connections" in inspector.get_table_names()
    assert "sender_accounts" in inspector.get_table_names()
    constraints = {constraint["name"] for constraint in inspector.get_unique_constraints("sender_accounts")}
    assert "uq_sender_accounts_tenant_connection_email" in constraints
    engine.dispose()


# --------------------------------------------------------------------- #
# Phase 10B: Google sender test-send endpoint
# --------------------------------------------------------------------- #
def test_send_test_email_with_explicit_recipient(integration_client) -> None:
    client, admin_token, *_ = integration_client
    connection_id = _create_google(client, admin_token).json()["id"]
    _store_google_tokens(client, admin_token, connection_id)
    sender_id = client.post(
        f"/api/v1/integrations/{connection_id}/senders",
        json={"email": "sender@example.com"},
        headers=_headers(admin_token),
    ).json()["id"]
    response = client.post(
        f"/api/v1/senders/{sender_id}/test",
        json={"recipient": "recipient@example.com"},
        headers=_headers(admin_token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["recipient"] == "recipient@example.com"
    assert body["message_id"] == "msg-test-1"
    assert body["provider"] == "GOOGLE"
    events = _audit_items(client, admin_token, "TEST_EMAIL_SENT")
    sent = [item for item in events if item["entity_id"] == sender_id and item["metadata"].get("success")]
    assert any(item["metadata"].get("recipient") == "recipient@example.com" for item in sent)


def test_send_test_email_rejects_invalid_recipient(integration_client) -> None:
    client, admin_token, *_ = integration_client
    connection_id = _create_google(client, admin_token).json()["id"]
    sender_id = client.post(
        f"/api/v1/integrations/{connection_id}/senders",
        json={"email": "sender@example.com"},
        headers=_headers(admin_token),
    ).json()["id"]
    response = client.post(
        f"/api/v1/senders/{sender_id}/test",
        json={"recipient": "not-an-email"},
        headers=_headers(admin_token),
    )
    assert response.status_code == 422


def test_send_test_email_requires_auth(integration_client) -> None:
    client, *_ = integration_client
    assert client.post(
        "/api/v1/senders/00000000-0000-0000-0000-000000000000/test",
        json={"recipient": "a@example.com"},
    ).status_code == 401


def test_duplicate_google_connection_rejected(integration_client) -> None:
    client, admin_token, *_ = integration_client
    first = _create_google(client, admin_token).json()
    assert first["status"] == "CONNECTING"
    second = _create_google(client, admin_token)
    assert second.status_code == 409


# --------------------------------------------------------------------- #
# Phase 10B: Google OAuth authorize (+ state-bound callback)
# --------------------------------------------------------------------- #
def test_google_authorize_requires_oauth_permission(integration_client) -> None:
    client, _admin_token, member_token, *_ = integration_client
    response = client.post(
        "/api/v1/senders/google/authorize",
        json={"connection_id": str(uuid4())},
        headers=_headers(member_token),
    )
    assert response.status_code == 403


def test_google_authorize_missing_connection_404(integration_client) -> None:
    client, admin_token, *_ = integration_client
    response = client.post(
        "/api/v1/senders/google/authorize",
        json={"connection_id": str(uuid4())},
        headers=_headers(admin_token),
    )
    assert response.status_code == 404
