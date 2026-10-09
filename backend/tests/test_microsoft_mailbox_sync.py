"""Phase 6 Microsoft 365 mailbox sync tests.

Covers: discovery→create for Microsoft Graph payloads (Member users, Guest
skip, suspended users), idempotent upsert, soft-deletion of absent mailboxes,
``last_sync_stats`` population (created/updated/suspended/deleted/skipped),
MICROSOFT_MAILBOX_SYNC_* audit lineage, and AUTH_REQUIRED → REVOKED on a
permanent token failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import get_db
from app.email_providers.connection_base import AUTH_REQUIRED, ProviderConnectionError
from app.email_providers.credentials import encrypt_credential_reference
from app.email_providers.mailbox_discovery import (
    DiscoveredMailbox,
    DiscoveryPage,
    DiscoveryResult,
)
from app.main import app
from app.models import (
    Base,
    Mailbox,
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

PASSWORD = "correct horse battery staple"
MS_TENANT_ID = "6f31cbbb-9b0a-441f-b8d4-6abdb8c92f10"


# ------------------------------------------------------------------ #
# Controllable discovery provider
# ------------------------------------------------------------------ #
CURRENT_DISCOVERY: DiscoveryResult | None = None
CURRENT_DISCOVERY_ERROR: Exception | None = None


class _FakeMicrosoftDiscoveryProvider:
    provider_name = "MICROSOFT"

    def discover(self, **kwargs: Any) -> DiscoveryResult:
        if CURRENT_DISCOVERY_ERROR is not None:
            raise CURRENT_DISCOVERY_ERROR
        if CURRENT_DISCOVERY is None:
            raise RuntimeError("test did not configure a discovery result")
        # Mirror the real Microsoft provider: only USER-type records become
        # sender-eligible mailboxes; Guests/OTHER are counted as skipped and
        # never surface in ``all_mailboxes``.
        eligible = [mb for mb in CURRENT_DISCOVERY.all_mailboxes if (mb.user_type or "").upper() == "USER"]
        skipped = len(CURRENT_DISCOVERY.all_mailboxes) - len(eligible)
        page = DiscoveryPage(mailboxes=eligible, skipped=skipped)
        return DiscoveryResult(pages=[page])


def _mb(mailbox_id: str, email: str, **overrides: Any) -> DiscoveredMailbox:
    defaults: dict[str, Any] = dict(
        provider_mailbox_id=mailbox_id,
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


def _result(*mailboxes: DiscoveredMailbox) -> DiscoveryResult:
    result = DiscoveryResult()
    result.pages.append(DiscoveryPage(mailboxes=list(mailboxes)))
    return result


def _audit_items(client, token, action):
    response = client.get(
        "/api/v1/admin/audit-logs",
        params={"action": action, "page_size": 100},
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 200
    return response.json()["items"]


# ------------------------------------------------------------------ #
# Fixture
# ------------------------------------------------------------------ #
@pytest.fixture()
def ms_sync_client(tmp_path):
    global CURRENT_DISCOVERY, CURRENT_DISCOVERY_ERROR
    CURRENT_DISCOVERY = None
    CURRENT_DISCOVERY_ERROR = None

    engine = create_engine(
        f"sqlite:///{tmp_path / 'ms_sync.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    tenant = Tenant(name="Gamma", slug=f"gamma-{uuid4().hex[:8]}")
    session.add(tenant)
    session.flush()

    admin_role = Role(tenant_id=tenant.id, name="Admin")
    connect_perm = Permission(key="integrations.connect", description="Connect integrations")
    read_perm = Permission(key="integrations.read", description="Read integrations")
    session.add_all([admin_role, connect_perm, read_perm])
    session.flush()

    admin = User(tenant_id=tenant.id, email="admin@example.com", password_hash=hash_password(PASSWORD), display_name="Admin")
    session.add(admin)
    session.flush()
    session.add_all([
        UserRole(tenant_id=tenant.id, user_id=admin.id, role_id=admin_role.id),
        RolePermission(role_id=admin_role.id, permission_id=connect_perm.id),
        RolePermission(role_id=admin_role.id, permission_id=read_perm.id),
    ])
    session.commit()

    payload: dict[str, Any] = {
        "access_token": "eyJ0.fake.ms-access",
        "refresh_token": "rt.fake-ms-refresh",
        "client_id": "microsoft-test-client-id",
        "scopes": [
            "openid", "profile", "email", "offline_access",
            "User.Read", "Directory.Read.All", "Organization.Read.All",
        ],
        "expires_at": (datetime.now(UTC) + timedelta(hours=1)).isoformat(),
    }
    reference = encrypt_credential_reference(settings.encryption_key, payload, version="v1")
    connection = ProviderConnection(
        tenant_id=tenant.id,
        provider="MICROSOFT",
        connection_type="OAUTH",
        provider_account_id=MS_TENANT_ID,
        workspace_domain="contoso.com",
        display_name="Microsoft 365 (Contoso Ltd)",
        status="CONNECTED",
        scopes=list(payload["scopes"]),
        credential_reference=reference,
        credential_version="v1",
        credential_expires_at=datetime.now(UTC) + timedelta(hours=1),
        connected_by=admin.id,
        connection_metadata={"microsoftTenantId": MS_TENANT_ID, "organizationName": "Contoso Ltd"},
    )
    session.add(connection)
    session.commit()
    connection_id = connection.id

    token, _ = TokenService(settings).create_access_token(admin.id, tenant.id, ["Admin"])
    session.close()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    yield client, token, tenant.id, connection_id, session_factory
    app.dependency_overrides.clear()
    engine.dispose()


@pytest.fixture(autouse=True)
def _patch_microsoft_discovery(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force inline sync and route discovery through the fake."""
    import app.core.config as config_mod

    settings_obj = config_mod.settings
    old = settings_obj.mailbox_sync_inline
    object.__setattr__(settings_obj, "mailbox_sync_inline", True)
    monkeypatch.setattr(
        "app.services.mailboxes.get_mailbox_discovery_provider",
        lambda name: _FakeMicrosoftDiscoveryProvider(),
    )
    yield
    object.__setattr__(settings_obj, "mailbox_sync_inline", old)


def _run_sync(client, token, connection_id):
    resp = client.post(
        f"/api/v1/provider-connections/{connection_id}/sync",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    return resp.json()


# ------------------------------------------------------------------ #
# 1. Discovery creates mailboxes + stats + MICROSOFT_MAILBOX_SYNC_* audits
# ------------------------------------------------------------------ #
def test_sync_creates_mailboxes_with_stats(ms_sync_client) -> None:
    client, token, _tenant, connection_id, session_factory = ms_sync_client
    global CURRENT_DISCOVERY
    CURRENT_DISCOVERY = _result(
        _mb("u-1", "alice@contoso.com", display_name="Alice Alpha", department="Sales"),
        _mb("u-2", "bob@contoso.com", first_name="Bob", last_name="Beta", is_suspended=True),
        _mb("u-3", "guest@contoso.com", user_type="OTHER"),
        _mb("u-4", "carol@contoso.com", user_type="Guest", is_suspended=True),
    )

    data = _run_sync(client, token, connection_id)
    assert data["last_sync_status"] == "COMPLETED"
    assert data["last_sync_stats"]["created"] == 2
    assert data["last_sync_stats"]["updated"] == 0
    assert data["last_sync_stats"]["suspended"] == 1
    assert data["last_sync_stats"]["deleted"] == 0
    assert data["last_sync_stats"]["skipped"] == 2
    assert data["last_sync_stats"]["errors"] == 0
    assert data["last_sync_stats"]["completed_at"]

    db = session_factory()
    try:
        mailboxes = db.scalars(
            select(Mailbox).where(Mailbox.provider_connection_id == connection_id)
        ).all()
        by_email = {m.email: m for m in mailboxes}
        assert set(by_email) == {"alice@contoso.com", "bob@contoso.com"}
        assert by_email["bob@contoso.com"].is_suspended is True
        assert by_email["bob@contoso.com"].provider_status == "SUSPENDED"
        assert by_email["alice@contoso.com"].provider_status == "ACTIVE"
        assert all(m.is_deleted is False for m in mailboxes)
    finally:
        db.close()

    assert len(_audit_items(client, token, "MICROSOFT_MAILBOX_SYNC_STARTED")) >= 1
    assert len(_audit_items(client, token, "MICROSOFT_MAILBOX_SYNC_COMPLETED")) >= 1
    assert len(_audit_items(client, token, "MAILBOX_DISCOVERED")) >= 1


# ------------------------------------------------------------------ #
# 2. Idempotent re-sync: updates, no duplicates
# ------------------------------------------------------------------ #
def test_resync_updates_and_reuses(ms_sync_client) -> None:
    client, token, _tenant, connection_id, session_factory = ms_sync_client
    global CURRENT_DISCOVERY
    CURRENT_DISCOVERY = _result(_mb("u-1", "alice@contoso.com", job_title="Engineer"))
    _run_sync(client, token, connection_id)

    CURRENT_DISCOVERY = _result(
        _mb("u-1", "alice@contoso.com", job_title="Senior Engineer"),
        _mb("u-2", "bob@contoso.com", department="Eng"),
    )
    data = _run_sync(client, token, connection_id)
    assert data["last_sync_stats"]["created"] == 1
    assert data["last_sync_stats"]["updated"] == 1

    db = session_factory()
    try:
        rows = db.scalars(
            select(Mailbox).where(Mailbox.provider_connection_id == connection_id)
        ).all()
        assert len(rows) == 2
        alice = next(m for m in rows if m.email == "alice@contoso.com")
        assert alice.job_title == "Senior Engineer"
        assert alice.is_deleted is False
    finally:
        db.close()


# ------------------------------------------------------------------ #
# 3. Absent mailboxes soft-deleted
# ------------------------------------------------------------------ #
def test_sync_soft_deletes_absent(ms_sync_client) -> None:
    client, token, _tenant, connection_id, session_factory = ms_sync_client
    global CURRENT_DISCOVERY
    CURRENT_DISCOVERY = _result(_mb("u-1", "alice@contoso.com"), _mb("u-2", "bob@contoso.com"))
    _run_sync(client, token, connection_id)

    CURRENT_DISCOVERY = _result(_mb("u-1", "alice@contoso.com"))
    data = _run_sync(client, token, connection_id)
    assert data["last_sync_stats"]["deleted"] == 1

    db = session_factory()
    try:
        bob = db.scalar(
            select(Mailbox).where(Mailbox.provider_connection_id == connection_id, Mailbox.email == "bob@contoso.com")
        )
        assert bob.is_deleted is True
        assert bob.provider_status == "DELETED"
    finally:
        db.close()


# ------------------------------------------------------------------ #
# 4. Permanent AUTH_REQUIRED → connection REVOKED + FAILED + errors count
# ------------------------------------------------------------------ #
def test_sync_permament_token_failure_revokes(ms_sync_client) -> None:
    client, token, _tenant, connection_id, _session_factory = ms_sync_client
    global CURRENT_DISCOVERY_ERROR
    CURRENT_DISCOVERY_ERROR = ProviderConnectionError(
        AUTH_REQUIRED, "Microsoft token expired"
    )

    resp = client.post(
        f"/api/v1/provider-connections/{connection_id}/sync",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert resp.status_code == 200
    data = resp.json()
    assert data["last_sync_status"] == "FAILED"
    assert data["last_sync_error"] == AUTH_REQUIRED
    assert data["status"] == "REVOKED"
    assert data["credential_configured"] is False
    assert data["last_sync_stats"]["errors"] == 1

    assert len(_audit_items(client, token, "MICROSOFT_MAILBOX_SYNC_FAILED")) >= 1


# ------------------------------------------------------------------ #
# 5. Transient failure → connection stays CONNECTED, still FAILED status
# ------------------------------------------------------------------ #
def test_sync_transient_failure_keeps_connected(ms_sync_client) -> None:
    client, token, _tenant, connection_id, _session_factory = ms_sync_client
    global CURRENT_DISCOVERY_ERROR
    CURRENT_DISCOVERY_ERROR = ProviderConnectionError("RATE_LIMITED", "Graph throttled us")

    data = _run_sync(client, token, connection_id)
    assert data["last_sync_status"] == "FAILED"
    assert data["last_sync_error"] == "RATE_LIMITED"
    assert data["status"] == "CONNECTED"
    assert data["credential_configured"] is True
    assert len(_audit_items(client, token, "MICROSOFT_MAILBOX_SYNC_FAILED")) >= 1
