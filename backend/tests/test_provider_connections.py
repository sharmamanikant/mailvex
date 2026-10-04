"""Security + workflow tests for Phase 1 provider connections.

Covers: unauthenticated 401, RBAC (member vs admin), OAuth state lifecycle
(valid, invalid, expired, reused), cancellation, token-exchange failure, tenant
isolation, duplicate avoidance, encrypted credential never in API response,
disconnect/revoke audit events, and PROVDER_* audit trail.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from unittest.mock import MagicMock
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.api.provider_connections import get_oauth_state_store
from app.core.config import settings
from app.core.database import get_db
from app.main import app
from app.models import Base, Permission, Role, RolePermission, Tenant, User, UserRole
from app.security.passwords import hash_password
from app.security.tokens import TokenService
from app.services.oauth_state_store import OAuthStateStore

PASSWORD = "correct horse battery staple"


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #
def _mint_token(user_id, tenant_id, roles) -> str:
    token, _ = TokenService(settings).create_access_token(user_id, tenant_id, roles)
    return token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


# ------------------------------------------------------------------ #
# Fake Google OAuth credentials + flow
# ------------------------------------------------------------------ #
class _FakeCredentials:
    def __init__(
        self,
        *,
        token: str = "ya29.fake-access",
        refresh_token: str = "1//fake-refresh",
        scopes: tuple[str, ...] | None = None,
        id_token_claims: dict[str, Any] | None = None,
        expiry: datetime | None = None,
    ) -> None:
        self.token = token
        self.refresh_token = refresh_token
        self.scopes = list(scopes or ())
        self.id_token = self._encode_id_token(id_token_claims or {})
        self.expiry = expiry or datetime.now(UTC) + timedelta(hours=1)
        self.token_uri = "https://oauth2.googleapis.com/token"
        self.client_id = "test-client-id"

    @staticmethod
    def _encode_id_token(claims: dict[str, Any]) -> str:
        import base64
        import json
        header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        return f"{header}.{payload}.fakesig"


class _FakeFlow:
    def __init__(
        self,
        *,
        scopes: list[str] | None = None,
        state: str = "",
        credentials: _FakeCredentials | None = None,
        fail_fetch: bool = False,
    ) -> None:
        self.scopes = scopes or []
        self.state = state
        self.redirect_uri = ""
        self.credentials = credentials or _FakeCredentials(scopes=scopes)
        self._fail_fetch = fail_fetch

    def authorization_url(self, **kwargs: Any) -> tuple[str, str]:
        return f"https://accounts.google.com/o/oauth2/auth?state={self.state}", self.state

    def fetch_token(self, *, code: str) -> None:
        if self._fail_fetch:
            raise RuntimeError("token exchange failed")


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
def provider_client(tmp_path):
    global _state_store
    _state_store = OAuthStateStore(memory_fallback=True)
    app.dependency_overrides[get_oauth_state_store] = _override_state_store

    engine = create_engine(
        f"sqlite:///{tmp_path / 'provider_connections.db'}",
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
    connect_perm = Permission(key="integrations.connect", description="Connect integrations")
    read_perm = Permission(key="integrations.read", description="Read integrations")
    disconnect_perm = Permission(key="integrations.disconnect", description="Disconnect integrations")
    b_admin_role = Role(tenant_id=tenant_b.id, name="Admin")
    b_member_role = Role(tenant_id=tenant_b.id, name="Member")
    session.add_all([admin_role, member_role, connect_perm, read_perm, disconnect_perm, b_admin_role, b_member_role])
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
        RolePermission(role_id=b_member_role.id, permission_id=read_perm.id),
        UserRole(tenant_id=tenant_b.id, user_id=b_admin.id, role_id=b_admin_role.id),
        UserRole(tenant_id=tenant_b.id, user_id=b_member.id, role_id=b_member_role.id),
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
    yield client, admin_token, member_token, b_admin_token, b_member_token, tenant_a.id, tenant_b.id
    app.dependency_overrides.clear()
    engine.dispose()


_state_store: OAuthStateStore = OAuthStateStore(memory_fallback=True)


# ------------------------------------------------------------------ #
# Monkeypatch Google OAuth Flow (network-free)
# ------------------------------------------------------------------ #
def _default_flow_factory(*args: Any, **kwargs: Any) -> _FakeFlow:
    scopes = kwargs.get("scopes")
    creds = _FakeCredentials(
        scopes=scopes,
        id_token_claims={
            "sub": "103719666309283745",
            "email": "admin@example.com",
            "email_verified": True,
            "hd": "example.com",
        },
    )
    return _FakeFlow(scopes=scopes, state=kwargs.get("state", ""), credentials=creds)


# Tests override this module-level factory for failure-mode scenarios.
CURRENT_FLOW_FACTORY = _default_flow_factory


@pytest.fixture(autouse=True)
def _patch_google_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the real Google OAuth flow with a controlled fake."""
    import app.email_providers.google.workspace as workspace_mod

    def _fake_build(*args: Any, **kwargs: Any) -> _FakeFlow:
        return CURRENT_FLOW_FACTORY(*args, **kwargs)

    flow_cls = MagicMock()
    flow_cls.from_client_config.side_effect = _fake_build
    monkeypatch.setattr(workspace_mod, "Flow", flow_cls)


# ------------------------------------------------------------------ #
# 1. Authentication
# ------------------------------------------------------------------ #
def test_unauthenticated_returns_401(provider_client) -> None:
    client, *_ = provider_client
    assert client.post("/api/v1/provider-connections/google/connect").status_code == 401
    assert client.get("/api/v1/provider-connections").status_code == 401
    assert client.get(f"/api/v1/provider-connections/{uuid4()}").status_code == 401
    assert client.delete(f"/api/v1/provider-connections/{uuid4()}").status_code == 401
    assert client.post(f"/api/v1/provider-connections/{uuid4()}/revoke").status_code == 401


# ------------------------------------------------------------------ #
# 2. RBAC: member lacks integrations.connect
# ------------------------------------------------------------------ #
def test_member_lacks_connect_permission(provider_client) -> None:
    client, _admin_token, member_token, *_ = provider_client
    assert client.post("/api/v1/provider-connections/google/connect", headers=_headers(member_token)).status_code == 403
    # member_token has integrations.read via role fixture
    assert client.get("/api/v1/provider-connections", headers=_headers(member_token)).status_code == 200
    # member does NOT have integrations.disconnect
    fake_id = uuid4()
    assert client.delete(f"/api/v1/provider-connections/{fake_id}", headers=_headers(member_token)).status_code == 403
    assert client.post(f"/api/v1/provider-connections/{fake_id}/revoke", headers=_headers(member_token)).status_code == 403


# ------------------------------------------------------------------ #
# 3. Start returns consent URL (admin with integrations.connect)
# ------------------------------------------------------------------ #
def test_start_google_returns_authorization_url(provider_client) -> None:
    client, admin_token, *_ = provider_client
    resp = client.post("/api/v1/provider-connections/google/connect", headers=_headers(admin_token))
    assert resp.status_code == 200
    assert "authorization_url" in resp.json()
    assert "accounts.google.com" in resp.json()["authorization_url"]


# ------------------------------------------------------------------ #
# 4. State lifecycle: valid callback → CONNECTED
# ------------------------------------------------------------------ #
def test_valid_callback_marks_connected(provider_client) -> None:
    client, admin_token, *_ = provider_client
    # Step 1: start
    start = client.post("/api/v1/provider-connections/google/connect", headers=_headers(admin_token))
    assert start.status_code == 200
    auth_url = start.json()["authorization_url"]
    # extract state from the fake URL (state parameter)
    from urllib.parse import parse_qs, urlparse
    parsed = urlparse(auth_url)
    state = parse_qs(parsed.query).get("state", [""])[0]
    # Step 2: callback with code
    resp = client.get(
        "/api/v1/provider-connections/google/callback",
        params={"code": "fake-code", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "status=connected" in resp.headers["location"]
    # Step 3: list → status CONNECTED
    listed = client.get("/api/v1/provider-connections", headers=_headers(admin_token))
    assert listed.status_code == 200
    items = listed.json()
    assert len(items) == 1
    assert items[0]["status"] == "CONNECTED"
    assert items[0]["provider"] == "GOOGLE"
    assert items[0]["credential_configured"] is True
    # credentials never appear in response
    for forbidden in ("credential_reference", "access_token", "refresh_token", "client_secret"):
        assert forbidden not in str(items[0])


# ------------------------------------------------------------------ #
# 5. Callback with error=access_denied → redirect error
# ------------------------------------------------------------------ #
def test_access_denied_redirects_error(provider_client) -> None:
    client, admin_token, *_ = provider_client
    resp = client.post("/api/v1/provider-connections/google/connect", headers=_headers(admin_token))
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(resp.json()["authorization_url"]).query).get("state", [""])[0]
    cb = client.get("/api/v1/provider-connections/google/callback", params={"error": "access_denied", "state": state}, follow_redirects=False)
    assert cb.status_code == 302
    assert "error=access_denied" in cb.headers["location"]
    # Connection remains CONNECTING
    listed = client.get("/api/v1/provider-connections", headers=_headers(admin_token)).json()
    assert listed[0]["status"] == "CONNECTING"


# ------------------------------------------------------------------ #
# 6. Invalid state → redirect error=state_invalid
# ------------------------------------------------------------------ #
def test_invalid_state_redirects_error(provider_client) -> None:
    client, _admin_token, *_ = provider_client
    resp = client.get(
        "/api/v1/provider-connections/google/callback",
        params={"code": "x", "state": "bogus-state"},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "error=state_invalid" in resp.headers["location"]


# ------------------------------------------------------------------ #
# 7. Expired state → redirect error=state_expired
# ------------------------------------------------------------------ #
def test_expired_state_redirects_error(provider_client) -> None:
    client, admin_token, *_ = provider_client
    start = client.post("/api/v1/provider-connections/google/connect", headers=_headers(admin_token))
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(start.json()["authorization_url"]).query).get("state", [""])[0]
    # Manually expire the state
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
    resp = client.get("/api/v1/provider-connections/google/callback", params={"code": "x", "state": state}, follow_redirects=False)
    assert resp.status_code == 302
    assert "error=state_expired" in resp.headers["location"]


# ------------------------------------------------------------------ #
# 8. State reuse → error on second use
# ------------------------------------------------------------------ #
def test_state_reuse_rejected(provider_client) -> None:
    client, admin_token, *_ = provider_client
    start = client.post("/api/v1/provider-connections/google/connect", headers=_headers(admin_token))
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(start.json()["authorization_url"]).query).get("state", [""])[0]
    # First use → success
    first = client.get("/api/v1/provider-connections/google/callback", params={"code": "code1", "state": state}, follow_redirects=False)
    assert first.status_code == 302
    assert "status=connected" in first.headers["location"]
    # Second use → state_invalid (state already consumed)
    second = client.get("/api/v1/provider-connections/google/callback", params={"code": "code2", "state": state}, follow_redirects=False)
    assert second.status_code == 302
    assert "error=state_invalid" in second.headers["location"]


# ------------------------------------------------------------------ #
# 9. Duplicate provider_account_id → DUPLICATE (no second row)
# ------------------------------------------------------------------ #
def test_duplicate_connection_consolidated(provider_client) -> None:
    client, admin_token, *_ = provider_client
    # Start and complete first
    s1 = client.post("/api/v1/provider-connections/google/connect", headers=_headers(admin_token))
    from urllib.parse import parse_qs, urlparse
    state1 = parse_qs(urlparse(s1.json()["authorization_url"]).query).get("state", [""])[0]
    client.get("/api/v1/provider-connections/google/callback", params={"code": "c1", "state": state1}, follow_redirects=False)
    listed = client.get("/api/v1/provider-connections", headers=_headers(admin_token)).json()
    assert len(listed) == 1
    # Start a second placeholder, complete same identity → consolidates onto the
    # existing CONNECTED row (idempotent), the orphan placeholder is removed.
    s2 = client.post("/api/v1/provider-connections/google/connect", headers=_headers(admin_token))
    state2 = parse_qs(urlparse(s2.json()["authorization_url"]).query).get("state", [""])[0]
    again = client.get("/api/v1/provider-connections/google/callback", params={"code": "c2", "state": state2}, follow_redirects=False)
    assert again.status_code == 302
    assert "status=connected" in again.headers["location"]
    # Still exactly 1 row, still CONNECTED with usable credentials
    rows = client.get("/api/v1/provider-connections", headers=_headers(admin_token)).json()
    assert len(rows) == 1
    assert rows[0]["status"] == "CONNECTED"
    assert rows[0]["credential_configured"] is True


# ------------------------------------------------------------------ #
# 10. Tenant isolation: tenant B cannot see/delete tenant A's connection
# ------------------------------------------------------------------ #
def test_tenant_isolation(provider_client) -> None:
    client, admin_token, _member_token, b_admin_token, *_ = provider_client
    s = client.post("/api/v1/provider-connections/google/connect", headers=_headers(admin_token))
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(s.json()["authorization_url"]).query).get("state", [""])[0]
    client.get("/api/v1/provider-connections/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    conn_id = client.get("/api/v1/provider-connections", headers=_headers(admin_token)).json()[0]["id"]
    # Tenant B sees nothing
    assert client.get("/api/v1/provider-connections", headers=_headers(b_admin_token)).json() == []
    assert client.get(f"/api/v1/provider-connections/{conn_id}", headers=_headers(b_admin_token)).status_code == 404
    assert client.delete(f"/api/v1/provider-connections/{conn_id}", headers=_headers(b_admin_token)).status_code == 404
    assert client.post(f"/api/v1/provider-connections/{conn_id}/revoke", headers=_headers(b_admin_token)).status_code == 404


# ------------------------------------------------------------------ #
# 11. Disconnect → DISCONNECTED + audit
# ------------------------------------------------------------------ #
def test_disconnect_tombstones_credentials(provider_client) -> None:
    client, admin_token, *_ = provider_client
    # Connect first
    s = client.post("/api/v1/provider-connections/google/connect", headers=_headers(admin_token))
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(s.json()["authorization_url"]).query).get("state", [""])[0]
    client.get("/api/v1/provider-connections/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    conn_id = client.get("/api/v1/provider-connections", headers=_headers(admin_token)).json()[0]["id"]
    # Disconnect
    disc = client.delete(f"/api/v1/provider-connections/{conn_id}", headers=_headers(admin_token))
    assert disc.status_code == 200
    assert disc.json()["status"] == "DISCONNECTED"
    assert disc.json()["credential_configured"] is False
    # Audit event present
    disconnect_events = _audit_items(client, admin_token, "PROVIDER_DISCONNECTED")
    assert any(item["entity_id"] == conn_id for item in disconnect_events)


# ------------------------------------------------------------------ #
# 12. Revoke → REVOKED + audit
# ------------------------------------------------------------------ #
def test_revoke_marks_revoked(provider_client) -> None:
    client, admin_token, *_ = provider_client
    s = client.post("/api/v1/provider-connections/google/connect", headers=_headers(admin_token))
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(s.json()["authorization_url"]).query).get("state", [""])[0]
    client.get("/api/v1/provider-connections/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    conn_id = client.get("/api/v1/provider-connections", headers=_headers(admin_token)).json()[0]["id"]
    rev = client.post(f"/api/v1/provider-connections/{conn_id}/revoke", headers=_headers(admin_token))
    assert rev.status_code == 200
    assert rev.json()["status"] == "REVOKED"
    assert rev.json()["credential_configured"] is False
    rev_events = _audit_items(client, admin_token, "PROVIDER_REVOKED")
    assert any(item["entity_id"] == conn_id for item in rev_events)


# ------------------------------------------------------------------ #
# 13. Audit PROVDER_CONNECTED + PROVDER_CONNECTION_STARTED recorded
# ------------------------------------------------------------------ #
def test_audit_events_recorded_for_start_and_complete(provider_client) -> None:
    client, admin_token, *_ = provider_client
    s = client.post("/api/v1/provider-connections/google/connect", headers=_headers(admin_token))
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(s.json()["authorization_url"]).query).get("state", [""])[0]
    started = _audit_items(client, admin_token, "PROVIDER_CONNECTION_STARTED")
    assert len(started) >= 1
    client.get("/api/v1/provider-connections/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    connected = _audit_items(client, admin_token, "PROVIDER_CONNECTED")
    assert len(connected) >= 1
    # No credentials leak in audit
    serialized = str([item["metadata"] for item in started + connected])
    assert "ya29" not in serialized
    assert "1//fake-refresh" not in serialized


# ------------------------------------------------------------------ #
# 14. Credential never exposed in any list or get response
# ------------------------------------------------------------------ #
def test_credentials_never_exposed_in_api(provider_client) -> None:
    client, admin_token, *_ = provider_client
    s = client.post("/api/v1/provider-connections/google/connect", headers=_headers(admin_token))
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(s.json()["authorization_url"]).query).get("state", [""])[0]
    client.get("/api/v1/provider-connections/google/callback", params={"code": "c", "state": state}, follow_redirects=False)
    listed = client.get("/api/v1/provider-connections", headers=_headers(admin_token)).json()
    for forbidden in ("credential_reference", "access_token", "refresh_token", "client_secret", "smtp_password", "api_key"):
        assert forbidden not in str(listed)


# ------------------------------------------------------------------ #
# 15. Token-exchange failure → PROVIDER_CONNECTION_FAILED audit + redirect
# ------------------------------------------------------------------ #
def test_exchange_failure_records_failed_audit(provider_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, admin_token, *_ = provider_client

    def _fail_factory(*args: Any, **kwargs: Any) -> _FakeFlow:
        return _FakeFlow(
            scopes=kwargs.get("scopes"),
            state=kwargs.get("state", ""),
            fail_fetch=True,
        )

    monkeypatch.setattr(__import__(__name__), "CURRENT_FLOW_FACTORY", _fail_factory)
    s = client.post("/api/v1/provider-connections/google/connect", headers=_headers(admin_token))
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(s.json()["authorization_url"]).query).get("state", [""])[0]
    resp = client.get("/api/v1/provider-connections/google/callback", params={"code": "bad", "state": state}, follow_redirects=False)
    assert resp.status_code == 302
    assert "error=oauth_failed" in resp.headers["location"]
    failed_events = _audit_items(client, admin_token, "PROVIDER_CONNECTION_FAILED")
    assert len(failed_events) >= 1


# ------------------------------------------------------------------ #
# 16. id_token missing sub/email → verify_failed
# ------------------------------------------------------------------ #
def test_missing_identity_returns_verify_failed(provider_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, admin_token, *_ = provider_client
    import app.email_providers.google.workspace as workspace_mod

    creds = _FakeCredentials(
        scopes=list(workspace_mod.GOOGLE_WORKSPACE_OAUTH_SCOPES),
        id_token_claims={},  # empty claims → no sub/email
    )

    def _no_identity_factory(*args: Any, **kwargs: Any) -> _FakeFlow:
        return _FakeFlow(
            scopes=kwargs.get("scopes"),
            state=kwargs.get("state", ""),
            credentials=creds,
        )

    monkeypatch.setattr(__import__(__name__), "CURRENT_FLOW_FACTORY", _no_identity_factory)
    s = client.post("/api/v1/provider-connections/google/connect", headers=_headers(admin_token))
    from urllib.parse import parse_qs, urlparse
    state = parse_qs(urlparse(s.json()["authorization_url"]).query).get("state", [""])[0]
    resp = client.get(
        "/api/v1/provider-connections/google/callback",
        params={"code": "x", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "error=verify_failed" in resp.headers["location"]


# ------------------------------------------------------------------ #
# 17. Missing code/state in callback → state_invalid
# ------------------------------------------------------------------ #
def test_missing_code_or_state_redirects_error(provider_client) -> None:
    client, *_ = provider_client
    # No code, no state
    resp = client.get("/api/v1/provider-connections/google/callback", follow_redirects=False)
    assert resp.status_code == 302
    assert "error=state_invalid" in resp.headers["location"]
    # code present but no state
    resp2 = client.get("/api/v1/provider-connections/google/callback", params={"code": "x"}, follow_redirects=False)
    assert resp2.status_code == 302
    assert "error=state_invalid" in resp2.headers["location"]
    # state present but no code
    resp3 = client.get("/api/v1/provider-connections/google/callback", params={"state": "s"}, follow_redirects=False)
    assert resp3.status_code == 302
    assert "error=state_invalid" in resp3.headers["location"]


# ------------------------------------------------------------------ #
# 18. Trailing-slash callback redirect helper
# ------------------------------------------------------------------ #
def test_trailing_slash_callback_redirect(provider_client) -> None:
    client, *_ = provider_client
    resp = client.get("/api/v1/provider-connections/google/callback/", follow_redirects=False)
    assert resp.status_code == 302
    assert "error=state_invalid" in resp.headers["location"]
    # With code+state → redirect param
    resp2 = client.get("/api/v1/provider-connections/google/callback/", params={"code": "c", "state": "s"}, follow_redirects=False)
    assert resp2.status_code == 302
    assert "redirect=1" in resp2.headers["location"]
    # With error=access_denied → access_denied
    resp3 = client.get("/api/v1/provider-connections/google/callback/", params={"error": "access_denied"}, follow_redirects=False)
    assert resp3.status_code == 302
    assert "error=access_denied" in resp3.headers["location"]


# ===================================================================== #
# 19-24  Credential refresh lifecycle
# ===================================================================== #

def _connect_via_api(client, token) -> str:
    """Run the full connect+callback flow and return the connection_id."""
    from urllib.parse import parse_qs, urlparse
    s = client.post("/api/v1/provider-connections/google/connect", headers=_headers(token))
    state = parse_qs(urlparse(s.json()["authorization_url"]).query).get("state", [""])[0]
    resp = client.get(
        "/api/v1/provider-connections/google/callback",
        params={"code": "x", "state": state},
        follow_redirects=False,
    )
    assert resp.status_code == 302
    assert "status=connected" in resp.headers["location"]
    # Get the connection from list
    listing = client.get("/api/v1/provider-connections", headers=_headers(token))
    connections = listing.json()
    assert len(connections) >= 1
    return connections[0]["id"]


# ------------------------------------------------------------------ #
# 19. Manual refresh succeeds → new version, refreshed audit
# ------------------------------------------------------------------ #
def test_refresh_credentials_succeeds(provider_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, admin_token, *_ = provider_client
    conn_id = _connect_via_api(client, admin_token)

    def _mock_refresh(self, *, credential_reference, credential_version):
        from app.email_providers.base import CredentialRotationResult
        return CredentialRotationResult(
            credential_reference="encrypted:new-refresh-v2",
            credential_version="v2",
            expires_at=datetime.now(UTC) + timedelta(hours=2),
        )

    monkeypatch.setattr(
        "app.email_providers.google.workspace.GoogleWorkspaceProviderConnection.refresh_credentials",
        _mock_refresh,
    )

    resp = client.post(
        f"/api/v1/provider-connections/{conn_id}/refresh",
        headers=_headers(admin_token),
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["status"] == "CONNECTED"
    assert data["credential_configured"] is True

    events = _audit_items(client, admin_token, "PROVIDER_REFRESHED")
    assert len(events) >= 1


# ------------------------------------------------------------------ #
# 20. Refresh with AUTH_REQUIRED → connection REVOKED + tombstoned
# ------------------------------------------------------------------ #
def test_refresh_credentials_revokes_on_auth_required(provider_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, admin_token, *_ = provider_client
    conn_id = _connect_via_api(client, admin_token)

    from app.email_providers.connection_base import ProviderConnectionError

    def _auth_required_refresh(self, *, credential_reference, credential_version):
        raise ProviderConnectionError("AUTH_REQUIRED", "Token expired")

    monkeypatch.setattr(
        "app.email_providers.google.workspace.GoogleWorkspaceProviderConnection.refresh_credentials",
        _auth_required_refresh,
    )

    resp = client.post(
        f"/api/v1/provider-connections/{conn_id}/refresh",
        headers=_headers(admin_token),
    )
    assert resp.status_code == 409  # AUTH_REQUIRED → 409

    events = _audit_items(client, admin_token, "PROVIDER_REFRESH_FAILED")
    assert len(events) >= 1

    # Verify the connection was revoked via GET
    get_resp = client.get(f"/api/v1/provider-connections/{conn_id}", headers=_headers(admin_token))
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "REVOKED"
    assert get_resp.json()["credential_configured"] is False


# ------------------------------------------------------------------ #
# 21. Refresh with REFRESH_FAILED (transient) → still CONNECTED
# ------------------------------------------------------------------ #
def test_refresh_credentials_stays_connected_on_transient_failure(provider_client, monkeypatch: pytest.MonkeyPatch) -> None:
    client, admin_token, *_ = provider_client
    conn_id = _connect_via_api(client, admin_token)

    from app.email_providers.connection_base import ProviderConnectionError

    def _transient_refresh(self, *, credential_reference, credential_version):
        raise ProviderConnectionError("REFRESH_FAILED", "Temporary network failure")

    monkeypatch.setattr(
        "app.email_providers.google.workspace.GoogleWorkspaceProviderConnection.refresh_credentials",
        _transient_refresh,
    )

    resp = client.post(
        f"/api/v1/provider-connections/{conn_id}/refresh",
        headers=_headers(admin_token),
    )
    assert resp.status_code == 400  # REFRESH_FAILED → 400

    # Verify the connection remains CONNECTED via API
    get_resp = client.get(f"/api/v1/provider-connections/{conn_id}", headers=_headers(admin_token))
    assert get_resp.status_code == 200
    assert get_resp.json()["status"] == "CONNECTED"
    assert get_resp.json()["credential_configured"] is True


# ------------------------------------------------------------------ #
# 22. Refresh endpoint returns 404 for nonexistent connection
# ------------------------------------------------------------------ #
def test_refresh_connection_not_found(provider_client) -> None:
    client, admin_token, *_ = provider_client
    resp = client.post(
        f"/api/v1/provider-connections/{uuid4()}/refresh",
        headers=_headers(admin_token),
    )
    assert resp.status_code == 404


# ------------------------------------------------------------------ #
# 23. Member without integrations.connect cannot refresh
# ------------------------------------------------------------------ #
def test_refresh_member_forbidden(provider_client) -> None:
    client, _, member_token, *_ = provider_client
    resp = client.post(
        f"/api/v1/provider-connections/{uuid4()}/refresh",
        headers=_headers(member_token),
    )
    assert resp.status_code == 403
