"""Security + workflow tests for Phase 6 Microsoft 365 provider connections.

Mirrors ``test_provider_connections.py`` (Google) for the org-level Microsoft
flow: authentication, RBAC, OAuth state lifecycle handled by the shared state
store, cancel/consent error paths, tenant isolation, duplicate avoidance,
MICROSOFT_* audit lineage, no credential leakage, masked org metadata, and the
no-op remote revoke design.

Token exchange / Graph calls are monkeypatched at the adapter boundary
(``handle_callback``) exactly as ``test_provider_connections.py`` fakes
``Flow``; the single-use OAuth state handling stays 100% real.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.provider_connections import get_oauth_state_store
from app.core.config import settings
from app.core.database import get_db
from app.email_providers.connection_base import (
    OAuthCallbackResult,
    ProviderConnectionError,
    ProviderIdentity,
)
from app.main import app
from app.models import (
    Base,
    Permission,
    ProviderConnection,
    Role,
    RolePermission,
    Tenant,
    User,
    UserRole,
)
from app.security.passwords import hash_password
from app.security.tokens import TokenService
from app.services.oauth_state_store import OAuthStateStore

PASSWORD = "correct horse battery staple"

# A Microsoft tenant id is a GUID (36 chars) — the API masks it.
MS_TENANT_ID = "6f31cbbb-9b0a-441f-b8d4-6abdb8c92f10"


def _mint_token(user_id, tenant_id, roles) -> str:
    token, _ = TokenService(settings).create_access_token(user_id, tenant_id, roles)
    return token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ #
# Controllable fake Microsoft callback adapter
# ------------------------------------------------------------------ #
def _make_callback_result(
    *,
    tenant_id: str = MS_TENANT_ID,
    scopes: tuple[str, ...] | None = None,
    metadata: dict[str, Any] | None = None,
) -> OAuthCallbackResult:
    from app.email_providers.microsoft.workspace import MICROSOFT_WORKSPACE_OAUTH_SCOPES

    granted = tuple(scopes or MICROSOFT_WORKSPACE_OAUTH_SCOPES)
    identity = ProviderIdentity(
        provider="MICROSOFT",
        provider_account_id=tenant_id,
        email="admin@contoso.com",
        workspace_domain="contoso.com",
        display_name="Microsoft 365 (Contoso Ltd)",
        scopes=granted,
    )
    return OAuthCallbackResult(
        identity=identity,
        credential_payload={
            "access_token": "eyJ0.fake.microsoft-access",
            "refresh_token": "rt.fake-microsoft-refresh",
            "client_id": "microsoft-test-client-id",
            "scopes": list(granted),
            "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
        },
        connection_metadata=metadata
        or {
            "microsoftTenantId": tenant_id,
            "organizationName": "Contoso Ltd",
            "defaultDomain": "contoso.com",
            "consent_admin_object_id": "oid-fake-admin",
            "consent_admin_email": "admin@contoso.com",
            "consent_admin_display_name": "Ada Admin",
        },
    )


CURRENT_CALLBACK: Any = None


def _default_callback(*args: Any, **kwargs: Any) -> OAuthCallbackResult:
    if CURRENT_CALLBACK is None:
        return _make_callback_result()
    if isinstance(CURRENT_CALLBACK, Exception):
        raise CURRENT_CALLBACK
    return CURRENT_CALLBACK(*args, **kwargs)


# ------------------------------------------------------------------ #
# Audit helpers
# ------------------------------------------------------------------ #
def _audit_items(client, token, action):
    response = client.get(
        "/api/v1/admin/audit-logs",
        params={"action": action, "page_size": 100},
        headers=_headers(token),
    )
    assert response.status_code == 200
    return response.json()["items"]


# ------------------------------------------------------------------ #
# Fixture: isolated sqlite DB + tenants/roles/users + state-store override
# ------------------------------------------------------------------ #
def _override_state_store():
    return _state_store


@pytest.fixture()
def ms_provider_client(tmp_path):
    global _state_store
    _state_store = OAuthStateStore(memory_fallback=True)
    app.dependency_overrides[get_oauth_state_store] = _override_state_store

    engine = create_engine(
        f"sqlite:///{tmp_path / 'ms_provider_connections.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    tenant_a = Tenant(name="Alpha", slug=f"alpha-{uuid4().hex[:8]}")
    tenant_b = Tenant(name="Beta", slug=f"beta-{uuid4().hex[:8]}")
    session.add_all([tenant_a, tenant_b])
    session.flush()

    admin_role = Role(tenant_id=tenant_a.id, name="Admin")
    member_role = Role(tenant_id=tenant_a.id, name="Member")
    b_admin_role = Role(tenant_id=tenant_b.id, name="Admin")
    b_member_role = Role(tenant_id=tenant_b.id, name="Member")
    connect_perm = Permission(key="integrations.connect", description="Connect integrations")
    read_perm = Permission(key="integrations.read", description="Read integrations")
    disconnect_perm = Permission(key="integrations.disconnect", description="Disconnect integrations")
    session.add_all(
        [admin_role, member_role, b_admin_role, b_member_role, connect_perm, read_perm, disconnect_perm]
    )
    session.flush()

    admin = User(tenant_id=tenant_a.id, email="admin@example.com", password_hash=hash_password(PASSWORD), display_name="Admin")
    member = User(tenant_id=tenant_a.id, email="member@example.com", password_hash=hash_password(PASSWORD), display_name="Member")
    b_admin = User(tenant_id=tenant_b.id, email="badmin@example.com", password_hash=hash_password(PASSWORD), display_name="Beta Admin")
    b_member = User(tenant_id=tenant_b.id, email="bmember@example.com", password_hash=hash_password(PASSWORD), display_name="Beta Member")
    session.add_all([admin, member, b_admin, b_member])
    session.flush()

    session.add_all([
        UserRole(tenant_id=tenant_a.id, user_id=admin.id, role_id=admin_role.id),
        UserRole(tenant_id=tenant_a.id, user_id=member.id, role_id=member_role.id),
        RolePermission(role_id=member_role.id, permission_id=read_perm.id),
        RolePermission(role_id=admin_role.id, permission_id=connect_perm.id),
        RolePermission(role_id=admin_role.id, permission_id=read_perm.id),
        RolePermission(role_id=admin_role.id, permission_id=disconnect_perm.id),
        UserRole(tenant_id=tenant_b.id, user_id=b_admin.id, role_id=b_admin_role.id),
        UserRole(tenant_id=tenant_b.id, user_id=b_member.id, role_id=b_member_role.id),
        RolePermission(role_id=b_member_role.id, permission_id=read_perm.id),
        RolePermission(role_id=b_admin_role.id, permission_id=connect_perm.id),
        RolePermission(role_id=b_admin_role.id, permission_id=read_perm.id),
        RolePermission(role_id=b_admin_role.id, permission_id=disconnect_perm.id),
    ])
    session.commit()

    admin_token = _mint_token(admin.id, tenant_a.id, ["Admin"])
    member_token = _mint_token(member.id, tenant_a.id, ["Member"])
    b_admin_token = _mint_token(b_admin.id, tenant_b.id, ["Admin"])
    b_member_token = _mint_token(b_member.id, tenant_b.id, ["Member"])

    session.close()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    yield (
        client,
        admin_token,
        member_token,
        b_admin_token,
        b_member_token,
        tenant_a.id,
        tenant_b.id,
        session_factory,
    )
    app.dependency_overrides.clear()
    engine.dispose()


_state_store: OAuthStateStore = OAuthStateStore(memory_fallback=True)


@pytest.fixture(autouse=True)
def _patch_microsoft_callback(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the Microsoft adapter's token exchange with a controllable fake."""
    global CURRENT_CALLBACK
    CURRENT_CALLBACK = None
    from app.email_providers.microsoft.workspace import MicrosoftWorkspaceProviderConnection

    monkeypatch.setattr(
        MicrosoftWorkspaceProviderConnection,
        "handle_callback",
        _default_callback,
    )


# ------------------------------------------------------------------ #
# Test helpers
# ------------------------------------------------------------------ #
def _start(client, token) -> str:
    resp = client.post("/api/v1/provider-connections/microsoft/connect", headers=_headers(token))
    assert resp.status_code == 200
    return resp.json()["authorization_url"]


def _state_of(auth_url: str) -> str:
    from urllib.parse import parse_qs, urlparse

    return parse_qs(urlparse(auth_url).query).get("state", [""])[0]


def _connect(client, token) -> str:
    """Start + complete and return connection_id."""
    state = _state_of(_start(client, token))
    cb = client.get(
        "/api/v1/provider-connections/microsoft/callback",
        params={"code": "ms-code", "state": state},
        follow_redirects=False,
    )
    assert cb.status_code == 302
    assert "status=connected" in cb.headers["location"]
    items = client.get("/api/v1/provider-connections", headers=_headers(token)).json()
    assert len(items) == 1
    return items[0]["id"]


# ------------------------------------------------------------------ #
# 1. Authentication
# ------------------------------------------------------------------ #
def test_unauthenticated_returns_401(ms_provider_client) -> None:
    client, *_ = ms_provider_client
    assert client.post("/api/v1/provider-connections/microsoft/connect").status_code == 401


# ------------------------------------------------------------------ #
# 2. RBAC
# ------------------------------------------------------------------ #
def test_member_lacks_connect_permission(ms_provider_client) -> None:
    client, _admin_token, member_token, *_ = ms_provider_client
    resp = client.post("/api/v1/provider-connections/microsoft/connect", headers=_headers(member_token))
    assert resp.status_code == 403
    assert client.get("/api/v1/provider-connections", headers=_headers(member_token)).status_code == 200


# ------------------------------------------------------------------ #
# 3. Start returns a Microsoft consent URL (no network)
# ------------------------------------------------------------------ #
def test_start_microsoft_returns_authorization_url(ms_provider_client) -> None:
    client, admin_token, *_ = ms_provider_client
    url = _start(client, admin_token)
    assert "login.microsoftonline.com" in url
    assert "/oauth2/v2.0/authorize" in url
    assert "client_id=" in url
    assert "state=" in url


# ------------------------------------------------------------------ #
# 4. Valid callback → CONNECTED with masked org metadata + MICROSOFT audits
# ------------------------------------------------------------------ #
def test_valid_callback_marks_connected(ms_provider_client) -> None:
    client, admin_token, *_ = ms_provider_client
    conn_id = _connect(client, admin_token)
    items = client.get("/api/v1/provider-connections", headers=_headers(admin_token)).json()
    assert len(items) == 1
    row = items[0]
    assert row["id"] == conn_id
    assert row["provider"] == "MICROSOFT"
    assert row["status"] == "CONNECTED"
    assert row["credential_configured"] is True
    assert row["workspace_domain"] == "contoso.com"
    assert row["provider_metadata"]["organizationName"] == "Contoso Ltd"
    assert row["provider_metadata"]["defaultDomain"] == "contoso.com"
    assert row["provider_metadata"]["microsoftTenantId"] == "6f31cb...2f10"
    # consent-admin identity is never exposed
    assert "consent_admin_email" not in row["provider_metadata"]
    assert "oid-fake-admin" not in str(row)
    assert row["last_sync_stats"] == {}
    # audit lineage
    assert len(_audit_items(client, admin_token, "MICROSOFT_CONNECTION_STARTED")) >= 1
    assert len(_audit_items(client, admin_token, "MICROSOFT_CONNECTED")) >= 1


# ------------------------------------------------------------------ #
# 5. Callback error=access_denied / admin_consent_required mapping
# ------------------------------------------------------------------ #
def test_access_denied_error_query_mapping(ms_provider_client) -> None:
    client, *_ = ms_provider_client
    cb = client.get(
        "/api/v1/provider-connections/microsoft/callback",
        params={"error": "access_denied"},
        follow_redirects=False,
    )
    assert cb.status_code == 302
    assert "error=access_denied" in cb.headers["location"]

    cb2 = client.get(
        "/api/v1/provider-connections/microsoft/callback",
        params={"error": "admin_consent_required"},
        follow_redirects=False,
    )
    assert cb2.status_code == 302
    assert "error=admin_consent_required" in cb2.headers["location"]

    cb3 = client.get(
        "/api/v1/provider-connections/microsoft/callback",
        params={"error": "consent_required"},
        follow_redirects=False,
    )
    assert "error=admin_consent_required" in cb3.headers["location"]


# ------------------------------------------------------------------ #
# 6/7/8. State lifecycle (invalid / expired / reuse) — shared store
# ------------------------------------------------------------------ #
def test_invalid_state_redirects_error(ms_provider_client) -> None:
    client, *_ = ms_provider_client
    resp = client.get(
        "/api/v1/provider-connections/microsoft/callback",
        params={"code": "x", "state": "bogus-state"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "error=state_invalid" in resp.headers["location"]


def test_expired_state_redirects_error(ms_provider_client) -> None:
    client, admin_token, *_ = ms_provider_client
    state = _state_of(_start(client, admin_token))
    from app.services.oauth_state_store import OAuthStateRecord

    now = datetime.now(UTC)
    record = OAuthStateRecord(
        tenant_id=uuid4(),
        user_id=uuid4(),
        connection_id=None,
        created_at=now - timedelta(hours=2),
        expires_at=now - timedelta(hours=1),
    )
    _state_store._memory[_state_store._key(state)] = record  # type: ignore[union-attr]
    resp = client.get(
        "/api/v1/provider-connections/microsoft/callback",
        params={"code": "x", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "error=state_expired" in resp.headers["location"]


def test_state_reuse_rejected(ms_provider_client) -> None:
    client, admin_token, *_ = ms_provider_client
    state = _state_of(_start(client, admin_token))
    first = client.get(
        "/api/v1/provider-connections/microsoft/callback",
        params={"code": "c1", "state": state},
        follow_redirects=False,
    )
    assert "status=connected" in first.headers["location"]
    second = client.get(
        "/api/v1/provider-connections/microsoft/callback",
        params={"code": "c2", "state": state},
        follow_redirects=False,
    )
    assert "error=state_invalid" in second.headers["location"]


# ------------------------------------------------------------------ #
# 9. Duplicate provider_account_id (same M365 tenant) → single row
# ------------------------------------------------------------------ #
def test_duplicate_connection_consolidated(ms_provider_client) -> None:
    client, admin_token, *_ = ms_provider_client
    _connect(client, admin_token)
    # Second start + complete with the same Microsoft tenant → same row.
    state = _state_of(_start(client, admin_token))
    again = client.get(
        "/api/v1/provider-connections/microsoft/callback",
        params={"code": "c2", "state": state},
        follow_redirects=False,
    )
    assert "status=connected" in again.headers["location"]
    rows = client.get("/api/v1/provider-connections", headers=_headers(admin_token)).json()
    assert len(rows) == 1
    assert rows[0]["status"] == "CONNECTED"
    assert rows[0]["credential_configured"] is True


# ------------------------------------------------------------------ #
# 10. Tenant isolation
# ------------------------------------------------------------------ #
def test_tenant_isolation(ms_provider_client) -> None:
    client, admin_token, _member_token, b_admin_token, *_ = ms_provider_client
    conn_id = _connect(client, admin_token)
    assert client.get("/api/v1/provider-connections", headers=_headers(b_admin_token)).json() == []
    assert client.get(f"/api/v1/provider-connections/{conn_id}", headers=_headers(b_admin_token)).status_code == 404
    assert client.delete(f"/api/v1/provider-connections/{conn_id}", headers=_headers(b_admin_token)).status_code == 404


# ------------------------------------------------------------------ #
# 11. Disconnect → DISCONNECTED + MICROSOFT_DISCONNECTED audit
# ------------------------------------------------------------------ #
def test_disconnect_tombstones_credentials(ms_provider_client) -> None:
    client, admin_token, *_ = ms_provider_client
    conn_id = _connect(client, admin_token)
    disc = client.delete(f"/api/v1/provider-connections/{conn_id}", headers=_headers(admin_token))
    assert disc.status_code == 200
    assert disc.json()["status"] == "DISCONNECTED"
    assert disc.json()["credential_configured"] is False
    events = _audit_items(client, admin_token, "MICROSOFT_DISCONNECTED")
    assert any(item["entity_id"] == conn_id for item in events)


# ------------------------------------------------------------------ #
# 12. Revoke → REVOKED (Microsoft revoke is a no-op locally) + audit
# ------------------------------------------------------------------ #
def test_revoke_marks_revoked(ms_provider_client) -> None:
    client, admin_token, *_ = ms_provider_client
    conn_id = _connect(client, admin_token)
    rev = client.post(f"/api/v1/provider-connections/{conn_id}/revoke", headers=_headers(admin_token))
    assert rev.status_code == 200
    assert rev.json()["status"] == "REVOKED"
    assert rev.json()["credential_configured"] is False
    events = _audit_items(client, admin_token, "MICROSOFT_REVOKED")
    assert any(item["entity_id"] == conn_id for item in events)


# ------------------------------------------------------------------ #
# 13. Exchange failure → MICROSOFT_CONNECTION_FAILED audit + oauth_failed
# ------------------------------------------------------------------ #
def test_exchange_failure_records_failed_audit(ms_provider_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, admin_token, *_ = ms_provider_client
    monkeypatch.setattr(
        __import__(__name__),
        "CURRENT_CALLBACK",
        ProviderConnectionError("OAUTH_EXCHANGE_FAILED", "exchange failed"),
    )
    state = _state_of(_start(client, admin_token))
    resp = client.get(
        "/api/v1/provider-connections/microsoft/callback",
        params={"code": "bad", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "error=oauth_failed" in resp.headers["location"]
    events = _audit_items(client, admin_token, "MICROSOFT_CONNECTION_FAILED")
    assert len(events) >= 1


# ------------------------------------------------------------------ #
# 14. Admin-consent refusal → admin_consent_required token + failed audit
# ------------------------------------------------------------------ #
def test_admin_consent_required_redirects_token(ms_provider_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, admin_token, *_ = ms_provider_client
    monkeypatch.setattr(
        __import__(__name__),
        "CURRENT_CALLBACK",
        ProviderConnectionError("ADMIN_CONSENT_REQUIRED", "admin consent needed"),
    )
    state = _state_of(_start(client, admin_token))
    resp = client.get(
        "/api/v1/provider-connections/microsoft/callback",
        params={"code": "x", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "error=admin_consent_required" in resp.headers["location"]
    assert len(_audit_items(client, admin_token, "MICROSOFT_CONNECTION_FAILED")) >= 1


# ------------------------------------------------------------------ #
# 15. Credentials never exposed
# ------------------------------------------------------------------ #
def test_credentials_never_exposed_in_api(ms_provider_client) -> None:
    client, admin_token, *_ = ms_provider_client
    _connect(client, admin_token)
    listed = client.get("/api/v1/provider-connections", headers=_headers(admin_token)).json()
    for forbidden in ("credential_reference", "access_token", "refresh_token", "client_secret", "microsoft.client-secret"):
        assert forbidden not in str(listed)
    assert "fake.microsoft-access" not in str(listed)
    assert "fake-microsoft-refresh" not in str(listed)


# ------------------------------------------------------------------ #
# 16. Credential refresh lifecycle — MICROSOFT_REFRESH* audits
# ------------------------------------------------------------------ #
def test_refresh_credentials_succeeds(ms_provider_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, admin_token, *_ = ms_provider_client
    conn_id = _connect(client, admin_token)

    def _mock_refresh(self, *, credential_reference, credential_version):
        from app.email_providers.base import CredentialRotationResult

        return CredentialRotationResult(
            credential_reference="encrypted:new-ms-refresh-v2",
            credential_version="v2",
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )

    monkeypatch.setattr(
        "app.email_providers.microsoft.workspace.MicrosoftWorkspaceProviderConnection.refresh_credentials",
        _mock_refresh,
    )
    resp = client.post(f"/api/v1/provider-connections/{conn_id}/refresh", headers=_headers(admin_token))
    assert resp.status_code == 200
    assert resp.json()["status"] == "CONNECTED"
    assert len(_audit_items(client, admin_token, "MICROSOFT_REFRESHED")) >= 1


def test_refresh_credentials_revokes_on_auth_required(ms_provider_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, admin_token, *_ = ms_provider_client
    conn_id = _connect(client, admin_token)

    def _auth_required(self, *, credential_reference, credential_version):
        raise ProviderConnectionError("AUTH_REQUIRED", "Token expired")

    monkeypatch.setattr(
        "app.email_providers.microsoft.workspace.MicrosoftWorkspaceProviderConnection.refresh_credentials",
        _auth_required,
    )
    resp = client.post(f"/api/v1/provider-connections/{conn_id}/refresh", headers=_headers(admin_token))
    assert resp.status_code == 409
    assert len(_audit_items(client, admin_token, "MICROSOFT_REFRESH_FAILED")) >= 1
    get_resp = client.get(f"/api/v1/provider-connections/{conn_id}", headers=_headers(admin_token))
    assert get_resp.json()["status"] == "REVOKED"
    assert get_resp.json()["credential_configured"] is False


def test_refresh_credentials_stays_connected_on_transient_failure(ms_provider_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, admin_token, *_ = ms_provider_client
    conn_id = _connect(client, admin_token)

    def _transient(self, *, credential_reference, credential_version):
        raise ProviderConnectionError("REFRESH_FAILED", "Temporary network failure")

    monkeypatch.setattr(
        "app.email_providers.microsoft.workspace.MicrosoftWorkspaceProviderConnection.refresh_credentials",
        _transient,
    )
    resp = client.post(f"/api/v1/provider-connections/{conn_id}/refresh", headers=_headers(admin_token))
    assert resp.status_code == 400
    get_resp = client.get(f"/api/v1/provider-connections/{conn_id}", headers=_headers(admin_token))
    assert get_resp.json()["status"] == "CONNECTED"


# ------------------------------------------------------------------ #
# 17. Missing code/state + trailing-slash redirect
# ------------------------------------------------------------------ #
def test_missing_code_or_state_redirects_error(ms_provider_client) -> None:
    client, *_ = ms_provider_client
    resp = client.get("/api/v1/provider-connections/microsoft/callback", follow_redirects=False)
    assert resp.status_code == 302
    assert "error=state_invalid" in resp.headers["location"]
    resp2 = client.get("/api/v1/provider-connections/microsoft/callback", params={"code": "x"}, follow_redirects=False)
    assert "error=state_invalid" in resp2.headers["location"]


def test_trailing_slash_callback_redirect(ms_provider_client) -> None:
    client, *_ = ms_provider_client
    resp = client.get("/api/v1/provider-connections/microsoft/callback/", follow_redirects=False)
    assert resp.status_code == 302
    assert "error=state_invalid" in resp.headers["location"]
    resp2 = client.get("/api/v1/provider-connections/microsoft/callback/", params={"code": "c", "state": "s"}, follow_redirects=False)
    assert "redirect=1" in resp2.headers["location"]
    resp3 = client.get("/api/v1/provider-connections/microsoft/callback/", params={"error": "consent_required"}, follow_redirects=False)
    assert "admin_consent_required" in resp3.headers["location"]


# ------------------------------------------------------------------ #
# 18. Adapter unit details (no network)
# ------------------------------------------------------------------ #
def test_authority_resolves_organizations_for_common() -> None:
    from app.email_providers.microsoft.workspace import MicrosoftWorkspaceProviderConnection

    adapter = MicrosoftWorkspaceProviderConnection()
    assert adapter._authority == "https://login.microsoftonline.com/organizations"


def test_validate_scopes_rejects_escalation(ms_provider_client) -> None:
    from app.email_providers.microsoft.workspace import (
        MICROSOFT_WORKSPACE_OAUTH_SCOPES,
        _validate_scopes,
    )

    requested = frozenset(MICROSOFT_WORKSPACE_OAUTH_SCOPES)
    # All granted scopes within the requested set + required covered → ok
    _validate_scopes({"scope": " ".join(MICROSOFT_WORKSPACE_OAUTH_SCOPES)}, requested)
    # Escalation (scope not requested) → INSUFFICIENT_SCOPE
    with pytest.raises(ProviderConnectionError) as exc:
        _validate_scopes({"scope": " ".join(MICROSOFT_WORKSPACE_OAUTH_SCOPES) + " Mail.Read"}, requested)
    assert exc.value.code == "INSUFFICIENT_SCOPE"
    # Missing a required scope → INSUFFICIENT_SCOPE
    with pytest.raises(ProviderConnectionError) as exc2:
        _validate_scopes({"scope": "openid profile email offline_access User.Read"}, requested)
    assert exc2.value.code == "INSUFFICIENT_SCOPE"


def test_decode_id_token_parses_claims() -> None:
    import base64
    import json

    from app.email_providers.microsoft.workspace import _decode_id_token

    claims = {"tid": MS_TENANT_ID, "oid": "oid-1", "email": "admin@contoso.com", "name": "Ada"}
    seg = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
    token = f"eyJhbGciOiJub25lIn0.{seg}.sig"
    assert _decode_id_token(token) == claims
    assert _decode_id_token("not-a-jwt") is None
    assert _decode_id_token(None) is None


def test_revoke_is_noop_on_microsoft(ms_provider_client) -> None:
    from app.email_providers.microsoft.workspace import (
        MICROSOFT_REVOKE_NOOP,
        MicrosoftWorkspaceProviderConnection,
    )

    assert MICROSOFT_REVOKE_NOOP is True
    adapter = MicrosoftWorkspaceProviderConnection()
    assert adapter.disconnect() is None
    assert adapter.revoke_token("any-refresh-token") is None


def test_connection_model_metadata_columns(ms_provider_client) -> None:
    """The `metadata`/`last_sync_stats` columns round-trip via the ORM.

    Org metadata incl. consent-admin identity is persisted on the connection
    record (never in API responses / audits), and the tenant id is stored in
    full here so it can be masked at the API boundary.
    """
    from sqlalchemy import select

    client, admin_token, *_b, tenant_a, _tenant_b, session_factory = ms_provider_client
    _connect(client, admin_token)
    db_session = session_factory()
    try:
        row = db_session.scalar(
            select(ProviderConnection).where(ProviderConnection.tenant_id == tenant_a)
        )
        assert row is not None
        assert row.connection_metadata["organizationName"] == "Contoso Ltd"
        assert row.connection_metadata["microsoftTenantId"] == MS_TENANT_ID
        assert row.connection_metadata.get("consent_admin_email") == "admin@contoso.com"
        assert row.last_sync_stats == {}
    finally:
        db_session.close()