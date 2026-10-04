"""Security + workflow tests for Phase 2 Google Workspace mailbox discovery.

Covers: authentication, RBAC (member vs admin), discovery→create, pagination,
idempotent upsert, attribute updates, new mailboxes, suspended users, soft
deletion of absent mailboxes, credential-expiry failure → REVOKED, transient
failure → still CONNECTED, tenant isolation, list filters/pagination, and the
guarantee that no Sender records / credentials are ever created or exposed.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, func, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import get_db
from app.email_providers.connection_base import (
    AUTH_REQUIRED,
    REFRESH_FAILED,
    ProviderConnectionError,
)
from app.email_providers.mailbox_discovery import (
    DiscoveredMailbox,
    DiscoveryPage,
    DiscoveryResult,
)
from app.main import app
from app.models import (
    Base,
    Permission,
    Role,
    RolePermission,
    SenderAccount,
    Tenant,
    User,
    UserRole,
)
from app.security.passwords import hash_password
from app.security.tokens import TokenService

PASSWORD = "correct horse battery staple"


# ------------------------------------------------------------------ #
# Helpers
# ------------------------------------------------------------------ #
def _mint_token(user_id, tenant_id, roles) -> str:
    token, _ = TokenService(settings).create_access_token(user_id, tenant_id, roles)
    return token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _mb(mailbox_id: str | int, email: str, **overrides: Any) -> DiscoveredMailbox:
    defaults: dict[str, Any] = dict(
        provider_mailbox_id=str(mailbox_id),
        email=email,
        display_name=None,
        first_name=None,
        last_name=None,
        department=None,
        job_title=None,
        user_type="USER",
        is_suspended=False,
        is_deleted=False,
    )
    defaults.update(overrides)
    return DiscoveredMailbox(**defaults)


def _result(*pages: list[DiscoveredMailbox]) -> DiscoveryResult:
    result = DiscoveryResult()
    for group in pages:
        result.pages.append(DiscoveryPage(mailboxes=group))
    return result


# ------------------------------------------------------------------ #
# Controllable mock discovery provider
# ------------------------------------------------------------------ #
CURRENT_DISCOVERY: DiscoveryResult | None = None
CURRENT_DISCOVERY_ERROR: Exception | None = None


class _FakeDiscoveryProvider:
    provider_name = "GOOGLE"

    def discover(self, **kwargs: Any) -> DiscoveryResult:
        if CURRENT_DISCOVERY_ERROR is not None:
            raise CURRENT_DISCOVERY_ERROR
        if CURRENT_DISCOVERY is None:
            raise RuntimeError("test did not configure a discovery result")
        return CURRENT_DISCOVERY


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
# Fixture: isolated sqlite DB + tenants/roles/users
# ------------------------------------------------------------------ #
@pytest.fixture()
def mailbox_client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'mailboxes.db'}",
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
    viewer_role = Role(tenant_id=tenant_a.id, name="Viewer")
    connect_perm = Permission(key="integrations.connect", description="Connect integrations")
    read_perm = Permission(key="integrations.read", description="Read integrations")
    b_admin_role = Role(tenant_id=tenant_b.id, name="Admin")
    session.add_all([admin_role, member_role, viewer_role, connect_perm, read_perm, b_admin_role])
    session.flush()

    admin = User(tenant_id=tenant_a.id, email="admin@example.com", password_hash=hash_password(PASSWORD), display_name="Admin")
    member = User(tenant_id=tenant_a.id, email="member@example.com", password_hash=hash_password(PASSWORD), display_name="Member")
    viewer = User(tenant_id=tenant_a.id, email="viewer@example.com", password_hash=hash_password(PASSWORD), display_name="Viewer")
    b_admin = User(tenant_id=tenant_b.id, email="badmin@example.com", password_hash=hash_password(PASSWORD), display_name="Beta Admin")
    session.add_all([admin, member, viewer, b_admin])
    session.flush()

    session.add_all([
        UserRole(tenant_id=tenant_a.id, user_id=admin.id, role_id=admin_role.id),
        UserRole(tenant_id=tenant_a.id, user_id=member.id, role_id=member_role.id),
        UserRole(tenant_id=tenant_a.id, user_id=viewer.id, role_id=viewer_role.id),
        RolePermission(role_id=admin_role.id, permission_id=connect_perm.id),
        RolePermission(role_id=admin_role.id, permission_id=read_perm.id),
        RolePermission(role_id=member_role.id, permission_id=read_perm.id),
        UserRole(tenant_id=tenant_b.id, user_id=b_admin.id, role_id=b_admin_role.id),
        RolePermission(role_id=b_admin_role.id, permission_id=connect_perm.id),
        RolePermission(role_id=b_admin_role.id, permission_id=read_perm.id),
    ])
    session.commit()

    admin_token = _mint_token(admin.id, tenant_a.id, ["Admin"])
    member_token = _mint_token(member.id, tenant_a.id, ["Member"])
    viewer_token = _mint_token(viewer.id, tenant_a.id, ["Viewer"])
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
    yield (
        client,
        admin_token,
        member_token,
        viewer_token,
        b_admin_token,
        tenant_a.id,
        tenant_b.id,
        session_factory,
    )
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture(autouse=True)
def _inline_sync(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force inline sync mode and plug in the mock discovery provider."""
    global CURRENT_DISCOVERY, CURRENT_DISCOVERY_ERROR
    CURRENT_DISCOVERY = None
    CURRENT_DISCOVERY_ERROR = None
    import app.core.config as config_mod

    settings_obj = config_mod.settings
    old = settings_obj.mailbox_sync_inline
    object.__setattr__(settings_obj, "mailbox_sync_inline", True)
    monkeypatch.setattr(
        "app.services.mailboxes.get_mailbox_discovery_provider",
        lambda name: _FakeDiscoveryProvider(),
    )
    yield
    object.__setattr__(settings_obj, "mailbox_sync_inline", old)


# ------------------------------------------------------------------ #
# Fake Google OAuth flow (network-free)
# ------------------------------------------------------------------ #
class _FakeFlow:
    def __init__(
        self,
        *,
        scopes: list[str] | None = None,
        state: str = "",
        credentials: Any | None = None,
    ) -> None:
        self.scopes = scopes or []
        self.state = state
        self.redirect_uri = ""
        self.credentials = credentials or _FakeCredentials(scopes=scopes)

    def authorization_url(self, **kwargs: Any) -> tuple[str, str]:
        return f"https://accounts.google.com/o/oauth2/auth?state={self.state}", self.state

    def fetch_token(self, *, code: str) -> None:
        return None


class _FakeCredentials:
    def __init__(
        self,
        *,
        token: str = "ya29.test-access",
        refresh_token: str = "1//test-refresh",
        scopes: list[str] | None = None,
        expiry: Any | None = None,
    ) -> None:
        import base64
        import json

        self.token = token
        self.refresh_token = refresh_token
        self.scopes = list(scopes or [])
        self.expiry = expiry or datetime.now(UTC) + timedelta(hours=1)
        self.token_uri = "https://oauth2.googleapis.com/token"
        self.client_id = "test-client-id"
        claims = {
            "sub": "103719666309283745",
            "email": "admin@example.com",
            "email_verified": True,
            "hd": "example.com",
        }
        header = base64.urlsafe_b64encode(json.dumps({"alg": "none"}).encode()).decode().rstrip("=")
        payload = base64.urlsafe_b64encode(json.dumps(claims).encode()).decode().rstrip("=")
        self.id_token = f"{header}.{payload}.fakesig"


@pytest.fixture(autouse=True)
def _patch_google_flow(monkeypatch: pytest.MonkeyPatch) -> None:
    """Replace the real Google OAuth flow with a controlled fake."""
    from unittest.mock import MagicMock

    import app.email_providers.google.workspace as workspace_mod

    def _fake_build(*args: Any, **kwargs: Any) -> _FakeFlow:
        state = kwargs.get("state", "")
        return _FakeFlow(scopes=kwargs.get("scopes"), state=state)

    flow_cls = MagicMock()
    flow_cls.from_client_config.side_effect = _fake_build
    monkeypatch.setattr(workspace_mod, "Flow", flow_cls)


# ------------------------------------------------------------------ #
# Setup helper: connect via API and return the connection id
# ------------------------------------------------------------------ #
def _connect_via_api(client, token) -> str:
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
    listing = client.get("/api/v1/provider-connections", headers=_headers(token))
    connections = listing.json()
    assert len(connections) >= 1
    return connections[0]["id"]


# ===================================================================== #
# 1. Authentication
# ===================================================================== #
def test_unauthenticated_returns_401(mailbox_client) -> None:
    client, *_ = mailbox_client
    fake = uuid4()
    assert client.post(f"/api/v1/provider-connections/{fake}/sync").status_code == 401
    assert client.get(f"/api/v1/provider-connections/{fake}/mailboxes").status_code == 401
    assert client.get(f"/api/v1/mailboxes/{fake}").status_code == 401


# ===================================================================== #
# 2. RBAC
# ===================================================================== #
def test_member_cannot_sync_but_can_list(mailbox_client) -> None:
    client, _admin_token, member_token, viewer_token, *_ = mailbox_client
    fake = uuid4()
    # member has integrations.read but not integrations.connect
    assert client.post(f"/api/v1/provider-connections/{fake}/sync", headers=_headers(member_token)).status_code == 403
    assert client.get(f"/api/v1/provider-connections/{fake}/mailboxes", headers=_headers(member_token)).status_code == 404
    # viewer has neither permission → denied
    assert client.get(f"/api/v1/provider-connections/{fake}/mailboxes", headers=_headers(viewer_token)).status_code == 403
    assert client.get(f"/api/v1/mailboxes/{fake}", headers=_headers(viewer_token)).status_code == 403


# ===================================================================== #
# 3. Sync success: discovery → create + completed sync metadata
# ===================================================================== #
def test_sync_creates_mailboxes_and_marks_completed(mailbox_client) -> None:
    client, admin_token, *_ = mailbox_client
    conn_id = _connect_via_api(client, admin_token)

    global CURRENT_DISCOVERY, CURRENT_DISCOVERY_ERROR
    CURRENT_DISCOVERY = _result(
        [_mb("u1", "alice@example.com", display_name="Alice", department="Engineering", job_title="Engineer")],
        [_mb("u2", "bob@example.com", display_name="Bob", is_suspended=False)],
    )
    CURRENT_DISCOVERY_ERROR = None

    sync = client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token))
    assert sync.status_code == 200
    data = sync.json()
    assert data["status"] == "CONNECTED"
    assert data["last_sync_status"] == "COMPLETED"
    assert data["last_sync_error"] is None
    assert data["last_sync_completed_at"] is not None

    listed = client.get(
        f"/api/v1/provider-connections/{conn_id}/mailboxes",
        params={"page_size": 200},
        headers=_headers(admin_token),
    )
    page = listed.json()
    assert page["total"] == 2
    emails = {item["email"] for item in page["items"]}
    assert emails == {"alice@example.com", "bob@example.com"}
    alice = next(item for item in page["items"] if item["email"] == "alice@example.com")
    assert alice["provider_status"] == "ACTIVE"
    assert alice["display_name"] == "Alice"
    assert alice["department"] == "Engineering"
    assert alice["job_title"] == "Engineer"

    # credentials never leak anywhere
    serialized = str((data, page))
    for forbidden in ("credential_reference", "access_token", "refresh_token", "client_secret", "revoked:"):
        assert forbidden not in serialized

    # audit trail present
    started = _audit_items(client, admin_token, "MAILBOX_SYNC_STARTED")
    completed = _audit_items(client, admin_token, "MAILBOX_SYNC_COMPLETED")
    assert len(started) >= 1
    assert len(completed) >= 1


# ===================================================================== #
# 4. Pagination: discovery spans multiple Directory pages
# ===================================================================== #
def test_sync_handles_multiple_pages(mailbox_client) -> None:
    client, admin_token, *_ = mailbox_client
    conn_id = _connect_via_api(client, admin_token)

    global CURRENT_DISCOVERY
    page1 = [_mb(f"u{i}", f"user{i}@example.com") for i in range(10)]
    page2 = [_mb(f"u{i}", f"user{i}@example.com") for i in range(10, 20)]
    page3 = [_mb(f"u{i}", f"user{i}@example.com") for i in range(20, 25)]
    CURRENT_DISCOVERY = _result(page1, page2, page3)

    assert client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)).status_code == 200
    listed = client.get(
        f"/api/v1/provider-connections/{conn_id}/mailboxes",
        params={"page_size": 200},
        headers=_headers(admin_token),
    ).json()
    assert listed["total"] == 25
    assert len(listed["items"]) == 25


# ===================================================================== #
# 5. Idempotency: second sync does not duplicate rows
# ===================================================================== #
def test_sync_idempotent_upsert(mailbox_client) -> None:
    client, admin_token, *_ = mailbox_client
    conn_id = _connect_via_api(client, admin_token)

    global CURRENT_DISCOVERY
    CURRENT_DISCOVERY = _result(
        [_mb("u1", "alice@example.com", display_name="Alice"), _mb("u2", "bob@example.com")]
    )
    assert client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)).status_code == 200
    assert client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)).status_code == 200

    listed = client.get(
        f"/api/v1/provider-connections/{conn_id}/mailboxes",
        params={"page_size": 200},
        headers=_headers(admin_token),
    ).json()
    assert listed["total"] == 2
    assert len(listed["items"]) == 2


# ===================================================================== #
# 6. Updates: attribute changes applied on re-discovery
# ===================================================================== #
def test_sync_updates_changed_attributes(mailbox_client) -> None:
    client, admin_token, *_ = mailbox_client
    conn_id = _connect_via_api(client, admin_token)

    global CURRENT_DISCOVERY
    CURRENT_DISCOVERY = _result(
        [_mb("u1", "alice@example.com", job_title="Engineer", department="Engineering")]
    )
    assert client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)).status_code == 200

    CURRENT_DISCOVERY = _result(
        [_mb("u1", "alice@example.com", job_title="Staff Engineer", department="Engineering")]
    )
    assert client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)).status_code == 200

    listed = client.get(
        f"/api/v1/provider-connections/{conn_id}/mailboxes",
        params={"page_size": 200},
        headers=_headers(admin_token),
    ).json()
    assert listed["total"] == 1
    assert listed["items"][0]["job_title"] == "Staff Engineer"


# ===================================================================== #
# 7. New mailboxes added; no Sender auto-creation
# ===================================================================== #
def test_new_mailbox_on_second_sync_without_sender_creation(mailbox_client) -> None:
    client, admin_token, *_ = mailbox_client
    conn_id = _connect_via_api(client, admin_token)

    global CURRENT_DISCOVERY
    CURRENT_DISCOVERY = _result([_mb("u1", "alice@example.com")])
    assert client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)).status_code == 200

    CURRENT_DISCOVERY = _result([_mb("u1", "alice@example.com"), _mb("u2", "carol@example.com")])
    assert client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)).status_code == 200

    listed = client.get(
        f"/api/v1/provider-connections/{conn_id}/mailboxes",
        params={"page_size": 200},
        headers=_headers(admin_token),
    ).json()
    assert listed["total"] == 2

    _, _, _, _, _, _, _, session_factory = mailbox_client
    with session_factory() as session:
        sender_count = session.scalar(select(func.count(SenderAccount.id))) or 0
    assert sender_count == 0  # Phase 2 must NOT create Sender records


# ===================================================================== #
# 8. Suspended user retained as SUSPENDED
# ===================================================================== #
def test_suspended_user_marked_suspended(mailbox_client) -> None:
    client, admin_token, *_ = mailbox_client
    conn_id = _connect_via_api(client, admin_token)

    global CURRENT_DISCOVERY
    CURRENT_DISCOVERY = _result(
        [
            _mb("u1", "dave@example.com", is_suspended=True),
            _mb("u2", "erin@example.com", is_suspended=False),
        ]
    )
    assert client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)).status_code == 200

    listed = client.get(
        f"/api/v1/provider-connections/{conn_id}/mailboxes",
        params={"status": "SUSPENDED", "page_size": 200},
        headers=_headers(admin_token),
    ).json()
    assert listed["total"] == 1
    assert listed["items"][0]["email"] == "dave@example.com"
    assert listed["items"][0]["provider_status"] == "SUSPENDED"
    assert listed["items"][0]["is_suspended"] is True


# ===================================================================== #
# 9. Absent users soft-deleted (not hard-deleted), then restored
# ===================================================================== #
def test_absent_mailbox_soft_deleted_and_restored(mailbox_client) -> None:
    client, admin_token, *_ = mailbox_client
    conn_id = _connect_via_api(client, admin_token)
    def mailboxes_api(**params: Any) -> Any:
        return client.get(
            f"/api/v1/provider-connections/{conn_id}/mailboxes",
            params={"page_size": 200, **params},
            headers=_headers(admin_token),
        ).json()

    global CURRENT_DISCOVERY
    CURRENT_DISCOVERY = _result([_mb("u1", "alice@example.com"), _mb("u2", "bob@example.com")])
    assert client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)).status_code == 200

    CURRENT_DISCOVERY = _result([_mb("u1", "alice@example.com")])  # bob no longer in directory
    assert client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)).status_code == 200

    deleted = mailboxes_api(status="DELETED")
    assert deleted["total"] == 1
    assert deleted["items"][0]["email"] == "bob@example.com"
    assert deleted["items"][0]["is_deleted"] is True

    # user reappears in directory → restored as ACTIVE
    CURRENT_DISCOVERY = _result([_mb("u1", "alice@example.com"), _mb("u2", "bob@example.com")])
    assert client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)).status_code == 200
    active = mailboxes_api(status="ACTIVE")
    assert active["total"] == 2


# ===================================================================== #
# 10. Credential expiry / revocation during sync → connection REVOKED
# ===================================================================== #
def test_sync_auth_required_revokes_connection(mailbox_client) -> None:
    client, admin_token, *_ = mailbox_client
    conn_id = _connect_via_api(client, admin_token)

    global CURRENT_DISCOVERY, CURRENT_DISCOVERY_ERROR
    CURRENT_DISCOVERY = None
    CURRENT_DISCOVERY_ERROR = ProviderConnectionError(AUTH_REQUIRED, "Token revoked")

    sync = client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token))
    assert sync.status_code == 200
    data = sync.json()
    assert data["status"] == "REVOKED"
    assert data["last_sync_status"] == "FAILED"
    assert data["last_sync_error"] == "AUTH_REQUIRED"
    assert data["credential_configured"] is False

    failed = _audit_items(client, admin_token, "MAILBOX_SYNC_FAILED")
    assert len(failed) >= 1


# ===================================================================== #
# 11. Transient failure → connection stays CONNECTED, status FAILED
# ===================================================================== #
def test_sync_transient_failure_keeps_connection(mailbox_client) -> None:
    client, admin_token, *_ = mailbox_client
    conn_id = _connect_via_api(client, admin_token)

    global CURRENT_DISCOVERY, CURRENT_DISCOVERY_ERROR
    CURRENT_DISCOVERY = None
    CURRENT_DISCOVERY_ERROR = ProviderConnectionError(
        REFRESH_FAILED, "Google Directory API error: 503"
    )

    sync = client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token))
    assert sync.status_code == 200
    data = sync.json()
    assert data["status"] == "CONNECTED"
    assert data["last_sync_status"] == "FAILED"
    assert data["last_sync_error"] == "REFRESH_FAILED"
    assert data["credential_configured"] is True


# ===================================================================== #
# 12. Tenant isolation: tenant B cannot see or sync tenant A's data
# ===================================================================== #
def test_tenant_isolation(mailbox_client) -> None:
    client, admin_token, *_ = mailbox_client
    _, _, _, _, b_admin_token, *_ = mailbox_client
    conn_id = _connect_via_api(client, admin_token)

    global CURRENT_DISCOVERY
    CURRENT_DISCOVERY = _result([_mb("u1", "alice@example.com")])
    assert client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)).status_code == 200

    mailbox_id = client.get(
        f"/api/v1/provider-connections/{conn_id}/mailboxes",
        headers=_headers(admin_token),
    ).json()["items"][0]["id"]

    assert client.post(
        f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(b_admin_token)
    ).status_code == 404
    assert client.get(
        f"/api/v1/provider-connections/{conn_id}/mailboxes", headers=_headers(b_admin_token)
    ).status_code == 404
    assert client.get(f"/api/v1/mailboxes/{mailbox_id}", headers=_headers(b_admin_token)).status_code == 404


# ===================================================================== #
# 13. List filters + pagination
# ===================================================================== #
def test_mailbox_list_search_filters_and_pagination(mailbox_client) -> None:
    client, admin_token, *_ = mailbox_client
    conn_id = _connect_via_api(client, admin_token)

    global CURRENT_DISCOVERY
    CURRENT_DISCOVERY = _result(
        [
            _mb("u1", "alice@example.com", display_name="Alice Alpha", department="Engineering"),
            _mb("u2", "bob@example.com", display_name="Bob Beta", department="Engineering"),
            _mb("u3", "charlie@example.com", display_name="Charlie", department="Sales", is_suspended=True),
            _mb("u4", "dana@example.com", display_name="Dana", department="Finance"),
        ]
    )
    assert client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)).status_code == 200

    url = f"/api/v1/provider-connections/{conn_id}/mailboxes"

    # search matches email + display name
    search = client.get(url, params={"search": "bob"}, headers=_headers(admin_token)).json()
    assert search["total"] == 1
    assert search["items"][0]["email"] == "bob@example.com"

    # department search
    dept = client.get(url, params={"search": "engineer"}, headers=_headers(admin_token)).json()
    assert dept["total"] == 2

    # status filter
    suspended = client.get(url, params={"status": "SUSPENDED"}, headers=_headers(admin_token)).json()
    assert suspended["total"] == 1
    assert suspended["items"][0]["email"] == "charlie@example.com"

    # pagination: page_size=2 page=2
    page2 = client.get(url, params={"page": 2, "page_size": 2}, headers=_headers(admin_token)).json()
    assert page2["total"] == 4
    assert len(page2["items"]) == 2


# ===================================================================== #
# 14. Sync ignores duplicate mailbox ids across pages (upsert dedupe)
# ===================================================================== #
def test_sync_last_discovered_refresh_and_connection_sync_fields(mailbox_client) -> None:
    client, admin_token, *_ = mailbox_client
    conn_id = _connect_via_api(client, admin_token)

    global CURRENT_DISCOVERY
    CURRENT_DISCOVERY = _result([_mb("u1", "alice@example.com")])
    assert client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)).status_code == 200

    conn = client.get(f"/api/v1/provider-connections/{conn_id}", headers=_headers(admin_token)).json()
    assert conn["last_sync_status"] == "COMPLETED"
    assert conn["last_sync_started_at"] is not None
    assert conn["last_sync_completed_at"] is not None
    assert conn["last_sync_at"] is not None


# ===================================================================== #
# 15. Sync on nonexistent / wrong-state connection
# ===================================================================== #
def test_sync_connection_not_found(mailbox_client) -> None:
    client, admin_token, *_ = mailbox_client
    resp = client.post(
        f"/api/v1/provider-connections/{uuid4()}/sync", headers=_headers(admin_token)
    )
    assert resp.status_code == 404


def test_list_mailboxes_connection_not_found(mailbox_client) -> None:
    client, admin_token, *_ = mailbox_client
    resp = client.get(
        f"/api/v1/provider-connections/{uuid4()}/mailboxes", headers=_headers(admin_token)
    )
    assert resp.status_code == 404


# ===================================================================== #
# 16. Production hardening: enqueue failure rolls back the SYNCING state
# ===================================================================== #
def test_sync_enqueue_failure_rolls_back_state_and_allows_retry(mailbox_client, monkeypatch) -> None:
    """If the celery queue is unreachable, the connection must not stay wedged
    in SYNCING: the state is reverted, a FAILED audit is recorded, and an
    immediate retry succeeds once the queue is healthy again."""
    client, admin_token, *_ = mailbox_client
    conn_id = _connect_via_api(client, admin_token)

    import app.tasks.scheduler as scheduler_mod
    from app.core.config import settings as cfg

    class _BrokenQueue:
        def delay(self, *args: Any, **kwargs: Any) -> None:
            raise RuntimeError("broker unreachable")

    monkeypatch.setattr(scheduler_mod, "sync_provider_mailboxes", _BrokenQueue())

    old_inline = cfg.mailbox_sync_inline
    object.__setattr__(cfg, "mailbox_sync_inline", False)
    try:
        resp = client.post(
            f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)
        )
        assert resp.status_code == 503

        conn = client.get(
            f"/api/v1/provider-connections/{conn_id}", headers=_headers(admin_token)
        ).json()
        assert conn["last_sync_status"] != "SYNCING"
        assert conn["last_sync_error"] == "QUEUE_UNAVAILABLE"

        # retry path is unblocked once the queue is healthy
        object.__setattr__(cfg, "mailbox_sync_inline", True)
        global CURRENT_DISCOVERY
        CURRENT_DISCOVERY = _result([_mb("u1", "alice@example.com")])
        ok = client.post(
            f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)
        )
        assert ok.status_code == 200
        assert ok.json()["last_sync_status"] == "COMPLETED"

        failed_events = _audit_items(client, admin_token, "MAILBOX_SYNC_FAILED")
        assert any(e["metadata"].get("reason") == "enqueue_failed" for e in failed_events)
    finally:
        object.__setattr__(cfg, "mailbox_sync_inline", old_inline)


# ===================================================================== #
# 17. Production hardening: search treats LIKE wildcards literally
# ===================================================================== #
def test_list_search_escapes_like_wildcards(mailbox_client) -> None:
    """`_` and `%` in a search term must match literally, not act as LIKE
    wildcards (otherwise `sales_team` would match `salesXteam`)."""
    client, admin_token, *_ = mailbox_client
    conn_id = _connect_via_api(client, admin_token)

    global CURRENT_DISCOVERY
    CURRENT_DISCOVERY = _result(
        [
            _mb("u1", "sales_team@example.com", display_name="Sales Team"),
            _mb("u2", "salesXteam@example.com", display_name="Sales X Team"),
            _mb("u3", "pricing100%off@example.com", display_name="Pricing"),
        ]
    )
    assert client.post(f"/api/v1/provider-connections/{conn_id}/sync", headers=_headers(admin_token)).status_code == 200

    url = f"/api/v1/provider-connections/{conn_id}/mailboxes"

    literal = client.get(url, params={"search": "sales_team"}, headers=_headers(admin_token)).json()
    assert [m["email"] for m in literal["items"]] == ["sales_team@example.com"]

    percent = client.get(url, params={"search": "100%"}, headers=_headers(admin_token)).json()
    assert [m["email"] for m in percent["items"]] == ["pricing100%off@example.com"]

    plain = client.get(url, params={"search": "sales"}, headers=_headers(admin_token)).json()
    assert {m["email"] for m in plain["items"]} == {"sales_team@example.com", "salesXteam@example.com"}