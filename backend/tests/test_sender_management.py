"""Phase 4 production-grade Sender management tests.

Covers the workspace Sender entity: list (search/filter/sort/pagination,
wildcard-escaping), detail (mailbox + provider summaries, availability, no
credential exposure), enable/disable with availability validation and audit,
soft remove, restore (with guardrails), RBAC, tenant isolation, and the
module-level ``get_sender_availability`` entry point the future email engine
will call before delivery.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.config import settings
from app.core.database import get_db
from app.main import app
from app.models import (
    Base,
    Mailbox,
    Permission,
    ProviderConnection,
    Role,
    RolePermission,
    Sender,
    Tenant,
    User,
    UserRole,
)
from app.security.passwords import hash_password
from app.security.tokens import TokenService
from app.services.workspace_senders import (
    SenderNotFoundError,
    get_sender_availability,
)

PASSWORD = "correct horse battery staple"


def _mint_token(user_id, tenant_id, roles) -> str:
    token, _ = TokenService(settings).create_access_token(user_id, tenant_id, roles)
    return token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _audit_items(client, token, action):
    response = client.get(
        "/api/v1/admin/audit-logs",
        params={"action": action, "page_size": 100},
        headers=_headers(token),
    )
    assert response.status_code == 200
    return response.json()["items"]


# ------------------------------------------------------------------ #
# Fixture: isolated sqlite DB + tenants/roles/users + seeded data
# ------------------------------------------------------------------ #
@pytest.fixture()
def sender_client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'senders.db'}",
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
    b_admin_role = Role(tenant_id=tenant_b.id, name="Admin")
    session.add_all([admin_role, member_role, connect_perm, read_perm, b_admin_role])
    session.flush()

    admin = User(tenant_id=tenant_a.id, email="admin@example.com", password_hash=hash_password(PASSWORD), display_name="Admin")
    member = User(tenant_id=tenant_a.id, email="member@example.com", password_hash=hash_password(PASSWORD), display_name="Member")
    b_admin = User(tenant_id=tenant_b.id, email="badmin@example.com", password_hash=hash_password(PASSWORD), display_name="Beta Admin")
    session.add_all([admin, member, b_admin])
    session.flush()

    session.add_all([
        UserRole(tenant_id=tenant_a.id, user_id=admin.id, role_id=admin_role.id),
        UserRole(tenant_id=tenant_a.id, user_id=member.id, role_id=member_role.id),
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
    b_admin_token = _mint_token(b_admin.id, tenant_b.id, ["Admin"])

    # ------------------------------------------------------------------ #
    # Deliverable data: connections, mailboxes, senders
    # ------------------------------------------------------------------ #
    conn1 = ProviderConnection(
        tenant_id=tenant_a.id,
        provider="GOOGLE",
        connection_type="OAUTH",
        status="CONNECTED",
        provider_account_id="acct-1",
        workspace_domain="example.com",
        credential_reference="encrypted:never-export-1",
    )
    conn2 = ProviderConnection(
        tenant_id=tenant_a.id,
        provider="GOOGLE",
        connection_type="OAUTH",
        status="DISCONNECTED",
        provider_account_id="acct-2",
        workspace_domain="deprecated.com",
    )
    conn_ms = ProviderConnection(
        tenant_id=tenant_a.id,
        provider="MICROSOFT",
        connection_type="OAUTH",
        status="CONNECTED",
        provider_account_id="acct-ms",
        workspace_domain="other.com",
        credential_reference="encrypted:never-export-2",
    )
    conn_revoked = ProviderConnection(
        tenant_id=tenant_a.id,
        provider="GOOGLE",
        connection_type="OAUTH",
        status="REVOKED",
        provider_account_id="acct-revoked",
        workspace_domain="revoked.com",
    )
    conn_b = ProviderConnection(
        tenant_id=tenant_b.id,
        provider="GOOGLE",
        connection_type="OAUTH",
        status="CONNECTED",
        provider_account_id="acct-b",
        workspace_domain="beta.com",
        credential_reference="encrypted:never-export-3",
    )
    session.add_all([conn1, conn2, conn_ms, conn_revoked, conn_b])
    session.flush()


    def _mailbox(conn, provider_id, email, *, status="ACTIVE", suspended=False, deleted=False):
        mb = Mailbox(
            tenant_id=conn.tenant_id,
            provider_connection_id=conn.id,
            provider_mailbox_id=provider_id,
            email=email,
            display_name=email.split("@")[0].capitalize(),
            department="Engineering",
            job_title="Engineer",
            provider_status=status,
            is_suspended=suspended,
            is_deleted=deleted,
            last_discovered_at=datetime.now(UTC),
        )
        session.add(mb)
        session.flush()
        return mb

    def _sender(mb, *, email=None, status="ACTIVE", enabled=False, health="UNKNOWN", score=None):
        s = Sender(
            tenant_id=mb.tenant_id,
            mailbox_id=mb.id,
            provider_connection_id=mb.provider_connection_id,
            email=email or mb.email,
            display_name=mb.display_name,
            provider=mb.provider_connection.provider if mb.provider_connection else "GOOGLE",
            status=status,
            sending_enabled=enabled,
            health_status=health,
            health_score=score,
        )
        session.add(s)
        session.flush()
        return s

    m_alice = _mailbox(conn1, "u-alice", "alice@example.com")
    m_bob = _mailbox(conn1, "u-bob", "bob@example.com")
    m_carol = _mailbox(conn1, "u-carol", "carol@example.com")
    m_charlie = _mailbox(conn1, "u-charlie", "charlie@example.com", status="SUSPENDED", suspended=True)
    m_dave = _mailbox(conn2, "u-dave", "dave@example.com")
    m_fiona = _mailbox(conn1, "u-fiona", "fiona@example.com")
    m_modern = _mailbox(conn1, "u-modern", "modern@example.com")
    m_dana = _mailbox(conn_ms, "u-dana", "dana@example.com")
    m_gina = _mailbox(conn1, "u-gina", "gina@example.com", status="DELETED", deleted=True)
    m_hank = _mailbox(conn_revoked, "u-hank", "hank@example.com")
    m_zoe = _mailbox(conn_b, "u-zoe", "zoe@example.com")

    s_alice = _sender(m_alice)
    s_bob = _sender(m_bob, enabled=True)
    _sender(m_carol, health="CRITICAL", score=Decimal("30.00"))
    s_charlie = _sender(m_charlie)
    s_dave = _sender(m_dave)
    s_fiona = _sender(m_fiona)
    _sender(m_modern, email="legacy@example.com")
    s_dana = _sender(m_dana)
    s_gina = _sender(m_gina, status="REMOVED")
    s_hank = _sender(m_hank, status="REMOVED")
    s_zoe = _sender(m_zoe, enabled=True)

    session.commit()

    seed = {
        "alice": s_alice.id,
        "bob": s_bob.id,
        "charlie": s_charlie.id,
        "dave": s_dave.id,
        "fiona": s_fiona.id,
        "gina": s_gina.id,
        "hank": s_hank.id,
        "dana": s_dana.id,
        "zoe": s_zoe.id,
        "mailbox_charlie": m_charlie.id,
        "tenant_a": tenant_a.id,
        "tenant_b": tenant_b.id,
    }

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
        seed,
        session_factory,
    )
    app.dependency_overrides.clear()
    engine.dispose()


# ===================================================================== #
# 1. Authentication
# ===================================================================== #
def test_unauthenticated_returns_401(sender_client) -> None:
    client, *_ = sender_client
    fake = uuid4()
    assert client.get("/api/v1/senders").status_code == 401
    assert client.get(f"/api/v1/senders/{fake}").status_code == 401
    assert client.patch(f"/api/v1/senders/{fake}", json={}).status_code == 401
    assert client.post(f"/api/v1/senders/{fake}/enable").status_code == 401
    assert client.post(f"/api/v1/senders/{fake}/disable").status_code == 401
    assert client.post(f"/api/v1/senders/{fake}/restore").status_code == 401
    assert client.delete(f"/api/v1/senders/{fake}").status_code == 401


# ===================================================================== #
# 2. RBAC: read allowed for member, writes require integrations.connect
# ===================================================================== #
def test_member_can_read_but_not_write(sender_client) -> None:
    client, _admin_token, member_token, *_ = sender_client
    fake = uuid4()
    assert client.get("/api/v1/senders", headers=_headers(member_token)).status_code == 200
    assert client.get(f"/api/v1/senders/{fake}", headers=_headers(member_token)).status_code == 404
    assert client.patch(f"/api/v1/senders/{fake}", json={}, headers=_headers(member_token)).status_code == 403
    assert client.post(f"/api/v1/senders/{fake}/enable", headers=_headers(member_token)).status_code == 403
    assert client.post(f"/api/v1/senders/{fake}/disable", headers=_headers(member_token)).status_code == 403
    assert client.post(f"/api/v1/senders/{fake}/restore", headers=_headers(member_token)).status_code == 403
    assert client.delete(f"/api/v1/senders/{fake}", headers=_headers(member_token)).status_code == 403


# ===================================================================== #
# 3. List: pagination + totals
# ===================================================================== #
def test_list_pagination_and_total_pages(sender_client) -> None:
    client, admin_token, *_ = sender_client

    page = client.get("/api/v1/senders", params={"page": 1, "page_size": 4}, headers=_headers(admin_token)).json()
    assert page["total"] == 10
    assert page["total_pages"] == 3
    assert len(page["items"]) == 4

    page2 = client.get("/api/v1/senders", params={"page": 2, "page_size": 4}, headers=_headers(admin_token)).json()
    assert page2["page"] == 2
    assert len(page2["items"]) == 4

    last = client.get("/api/v1/senders", params={"page": 3, "page_size": 4}, headers=_headers(admin_token)).json()
    assert len(last["items"]) == 2


# ===================================================================== #
# 4. List: every item carries effective availability (no secrets)
# ===================================================================== #
def test_list_includes_availability_and_no_secrets(sender_client) -> None:
    client, admin_token, *_ = sender_client
    page = client.get("/api/v1/senders", params={"page_size": 200}, headers=_headers(admin_token)).json()
    assert page["total"] == 10
    for item in page["items"]:
        assert "availability" in item
        assert "reason" in item["availability"]
        assert "available" in item["availability"]
    serialized = str(page)
    for forbidden in ("credential_reference", "access_token", "refresh_token", "client_secret", "encrypted:"):
        assert forbidden not in serialized

    # bob (enabled) is available; alice (not enabled) reports SENDER_DISABLED
    by_email = {item["email"]: item for item in page["items"]}
    assert by_email["bob@example.com"]["availability"]["available"] is True
    assert by_email["bob@example.com"]["availability"]["sending_enabled"] is True
    assert by_email["alice@example.com"]["availability"]["available"] is False
    assert by_email["alice@example.com"]["availability"]["reason"] == "SENDER_DISABLED"


# ===================================================================== #
# 5. List: search (sender email, display name, mailbox email, wildcards)
# ===================================================================== #
def test_list_search_fields_and_wildcard_escaping(sender_client) -> None:
    client, admin_token, *_ = sender_client

    # sender email
    by_sender = client.get("/api/v1/senders", params={"search": "alice", "page_size": 200}, headers=_headers(admin_token)).json()
    assert [i["email"] for i in by_sender["items"]] == ["alice@example.com"]

    # display-name search (sender display name = capitalized local part)
    by_display = client.get("/api/v1/senders", params={"search": "Fiona", "page_size": 200}, headers=_headers(admin_token)).json()
    assert [i["email"] for i in by_display["items"]] == ["fiona@example.com"]

    # mailbox email differs from sender email (mailbox renamed post-creation)
    by_mailbox = client.get("/api/v1/senders", params={"search": "modern", "page_size": 200}, headers=_headers(admin_token)).json()
    assert [i["email"] for i in by_mailbox["items"]] == ["legacy@example.com"]

    # wildcards are matched literally, never as LIKE globs
    escaped = client.get("/api/v1/senders", params={"search": "example", "page_size": 200}, headers=_headers(admin_token)).json()
    assert escaped["total"] == 10
    partial = client.get("/api/v1/senders", params={"search": "ex_ample", "page_size": 200}, headers=_headers(admin_token)).json()
    assert partial["total"] == 0
    percent = client.get("/api/v1/senders", params={"search": "100%", "page_size": 200}, headers=_headers(admin_token)).json()
    assert percent["total"] == 0


# ===================================================================== #
# 6. List: filters (provider, status, sending, health_status)
# ===================================================================== #
def test_list_filters(sender_client) -> None:
    client, admin_token, *_ = sender_client
    h = _headers(admin_token)

    provider = client.get("/api/v1/senders", params={"provider": "microsoft"}, headers=h).json()
    assert provider["total"] == 1
    assert provider["items"][0]["email"] == "dana@example.com"

    removed = client.get("/api/v1/senders", params={"status": "removed"}, headers=h).json()
    assert sorted(i["email"] for i in removed["items"]) == ["gina@example.com", "hank@example.com"]

    sending_on = client.get("/api/v1/senders", params={"sending": "true"}, headers=h).json()
    assert sorted(i["email"] for i in sending_on["items"]) == ["bob@example.com"]

    critical = client.get("/api/v1/senders", params={"health_status": "critical"}, headers=h).json()
    assert [i["email"] for i in critical["items"]] == ["carol@example.com"]


# ===================================================================== #
# 7. List: allow-listed server-side sorting (injection-safe fallback)
# ===================================================================== #
def test_list_sort_allowed_and_invalid_fallback(sender_client) -> None:
    client, admin_token, *_ = sender_client
    h = _headers(admin_token)

    asc = client.get("/api/v1/senders", params={"sort": "email", "page_size": 200}, headers=h).json()
    emails = [i["email"] for i in asc["items"]]
    assert emails == sorted(emails)

    desc = client.get("/api/v1/senders", params={"sort": "-email", "page_size": 200}, headers=h).json()
    desc_emails = [i["email"] for i in desc["items"]]
    assert desc_emails == sorted(emails, reverse=True)

    # invalid / hostile sort tokens fall back to the default (email asc), never error
    for bad in ("email;DROP TABLE senders", "password", "health_score DESC"):
        resp = client.get("/api/v1/senders", params={"sort": bad, "page_size": 200}, headers=h)
        assert resp.status_code == 200
        emails = [i["email"] for i in resp.json()["items"]]
        assert emails == sorted(emails)

    # enabled-if-sent via "-created_at" token also deterministic (no crash)
    by_created = client.get("/api/v1/senders", params={"sort": "-created_at", "page_size": 200}, headers=h)
    assert by_created.status_code == 200
    assert len(by_created.json()["items"]) == 10


# ===================================================================== #
# 8. Detail: sender + mailbox + provider + availability + health
# ===================================================================== #
def test_detail_returns_full_relationship_view(sender_client) -> None:
    client, admin_token, *_ = sender_client
    seed = sender_client[4]
    resp = client.get(f"/api/v1/senders/{seed['alice']}", headers=_headers(admin_token))
    assert resp.status_code == 200
    data = resp.json()

    assert data["email"] == "alice@example.com"
    assert data["status"] == "ACTIVE"
    assert data["sending_enabled"] is False
    assert data["availability"]["available"] is False
    assert data["availability"]["reason"] == "SENDER_DISABLED"
    assert data["availability"]["sender_status"] == "ACTIVE"
    assert data["availability"]["mailbox_status"] == "ACTIVE"
    assert data["availability"]["provider_connection_status"] == "CONNECTED"

    assert data["mailbox"]["email"] == "alice@example.com"
    assert data["mailbox"]["status"] == "ACTIVE"
    assert data["mailbox"]["department"] == "Engineering"
    assert data["mailbox"]["is_suspended"] is False
    assert data["mailbox"]["is_deleted"] is False

    assert data["provider_connection"]["provider"] == "GOOGLE"
    assert data["provider_connection"]["status"] == "CONNECTED"
    assert data["provider_connection"]["workspace_domain"] == "example.com"

    assert data["health"]["status"] == "UNKNOWN"
    assert data["health"]["score"] is None
    assert data["health"]["last_checked_at"] is None

    # no credentials or encrypted blobs anywhere
    serialized = str(data)
    for forbidden in ("credential_reference", "access_token", "refresh_token", "client_secret", "encrypted:"):
        assert forbidden not in serialized


def test_detail_not_found_for_foreign_or_missing_sender(sender_client) -> None:
    client, admin_token, *_ = sender_client
    assert client.get(f"/api/v1/senders/{uuid4()}", headers=_headers(admin_token)).status_code == 404


# ===================================================================== #
# 9. Enable / disable lifecycle with availability validation
# ===================================================================== #
def test_enable_succeeds_and_audits(sender_client) -> None:
    client, admin_token, *_ = sender_client
    seed = sender_client[4]
    resp = client.post(f"/api/v1/senders/{seed['alice']}/enable", headers=_headers(admin_token))
    assert resp.status_code == 200
    assert resp.json()["message"] == "Sending enabled"

    detail = client.get(f"/api/v1/senders/{seed['alice']}", headers=_headers(admin_token)).json()
    assert detail["sending_enabled"] is True
    assert detail["availability"]["available"] is True
    assert detail["availability"]["reason"] is None

    events = _audit_items(client, admin_token, "SENDER_ENABLED")
    assert any(str(item["entity_id"]) == str(seed["alice"]) for item in events)


def test_enable_blocked_by_suspended_mailbox(sender_client) -> None:
    client, admin_token, *_ = sender_client
    seed = sender_client[4]
    resp = client.post(f"/api/v1/senders/{seed['charlie']}/enable", headers=_headers(admin_token))
    assert resp.status_code == 409
    assert "suspended" in resp.json()["detail"].lower()

    events = _audit_items(client, admin_token, "SENDER_AVAILABILITY_BLOCKED")
    blocked = [e for e in events if e["entity_id"] == str(seed["charlie"])]
    assert len(blocked) == 1
    assert blocked[0]["metadata"]["reason"] == "MAILBOX_SUSPENDED"

    # sender still disabled afterwards
    detail = client.get(f"/api/v1/senders/{seed['charlie']}", headers=_headers(admin_token)).json()
    assert detail["sending_enabled"] is False


def test_enable_blocked_by_disconnected_provider(sender_client) -> None:
    client, admin_token, *_ = sender_client
    seed = sender_client[4]
    resp = client.post(f"/api/v1/senders/{seed['dave']}/enable", headers=_headers(admin_token))
    assert resp.status_code == 409
    assert "provider connection" in resp.json()["detail"].lower()

    events = _audit_items(client, admin_token, "SENDER_AVAILABILITY_BLOCKED")
    blocked = [e for e in events if e["entity_id"] == str(seed["dave"])]
    assert len(blocked) == 1
    assert blocked[0]["metadata"]["reason"] == "PROVIDER_DISCONNECTED"


def test_enable_blocked_for_removed_sender(sender_client) -> None:
    client, admin_token, *_ = sender_client
    seed = sender_client[4]
    resp = client.post(f"/api/v1/senders/{seed['gina']}/enable", headers=_headers(admin_token))
    assert resp.status_code == 409
    assert "removed" in resp.json()["detail"].lower()


def test_disable_succeeds_and_audits(sender_client) -> None:
    client, admin_token, *_ = sender_client
    seed = sender_client[4]
    resp = client.post(f"/api/v1/senders/{seed['bob']}/disable", headers=_headers(admin_token))
    assert resp.status_code == 200
    assert resp.json()["message"] == "Sending disabled"

    detail = client.get(f"/api/v1/senders/{seed['bob']}", headers=_headers(admin_token)).json()
    assert detail["sending_enabled"] is False
    assert detail["availability"]["available"] is False
    assert detail["availability"]["reason"] == "SENDER_DISABLED"

    events = _audit_items(client, admin_token, "SENDER_DISABLED")
    assert any(str(item["entity_id"]) == str(seed["bob"]) for item in events)


def test_patch_enable_and_display_name(sender_client) -> None:
    client, admin_token, *_ = sender_client
    seed = sender_client[4]
    resp = client.patch(
        f"/api/v1/senders/{seed['alice']}",
        json={"sending_enabled": True, "display_name": "Alice A"},
        headers=_headers(admin_token),
    )
    assert resp.status_code == 200
    assert resp.json()["display_name"] == "Alice A"
    assert resp.json()["sending_enabled"] is True
    assert resp.json()["availability"]["available"] is True


# ===================================================================== #
# 10. Soft remove + restore
# ===================================================================== #
def test_remove_soft_deletes_and_audits(sender_client) -> None:
    client, admin_token, *_ = sender_client
    seed = sender_client[4]
    resp = client.delete(f"/api/v1/senders/{seed['fiona']}", headers=_headers(admin_token))
    assert resp.status_code == 204

    detail = client.get(f"/api/v1/senders/{seed['fiona']}", headers=_headers(admin_token)).json()
    assert detail["status"] == "REMOVED"
    assert detail["sending_enabled"] is False
    assert detail["availability"]["available"] is False
    assert detail["availability"]["reason"] == "SENDER_REMOVED"

    events = _audit_items(client, admin_token, "SENDER_REMOVED")
    assert any(str(item["entity_id"]) == str(seed["fiona"]) for item in events)


def test_restore_reactivates_with_sending_off(sender_client) -> None:
    client, admin_token, *_ = sender_client
    seed = sender_client[4]
    assert client.delete(f"/api/v1/senders/{seed['fiona']}", headers=_headers(admin_token)).status_code == 204

    resp = client.post(f"/api/v1/senders/{seed['fiona']}/restore", headers=_headers(admin_token))
    assert resp.status_code == 200
    assert resp.json()["message"] == "Sender restored; sending remains disabled"

    detail = client.get(f"/api/v1/senders/{seed['fiona']}", headers=_headers(admin_token)).json()
    assert detail["status"] == "ACTIVE"
    assert detail["sending_enabled"] is False
    assert detail["availability"]["available"] is False
    assert detail["availability"]["reason"] == "SENDER_DISABLED"

    events = _audit_items(client, admin_token, "SENDER_RESTORED")
    assert any(str(item["entity_id"]) == str(seed["fiona"]) for item in events)


def test_restore_blocked_when_mailbox_deleted(sender_client) -> None:
    client, admin_token, *_ = sender_client
    seed = sender_client[4]
    resp = client.post(f"/api/v1/senders/{seed['gina']}/restore", headers=_headers(admin_token))
    assert resp.status_code == 409
    assert "mailbox" in resp.json()["detail"].lower()


def test_restore_blocked_when_connection_revoked(sender_client) -> None:
    client, admin_token, *_ = sender_client
    seed = sender_client[4]
    resp = client.post(f"/api/v1/senders/{seed['hank']}/restore", headers=_headers(admin_token))
    assert resp.status_code == 409
    assert "revoked" in resp.json()["detail"].lower()


def test_restore_blocked_for_active_sender(sender_client) -> None:
    client, admin_token, *_ = sender_client
    seed = sender_client[4]
    resp = client.post(f"/api/v1/senders/{seed['bob']}/restore", headers=_headers(admin_token))
    assert resp.status_code == 409
    assert "removed" in resp.json()["detail"].lower()


# ===================================================================== #
# 11. Tenant isolation
# ===================================================================== #
def test_tenant_isolation(sender_client) -> None:
    client, _admin_token, _, b_admin_token, seed, _ = sender_client
    # Tenancy A's whole collection is invisible to tenant B
    listed = client.get("/api/v1/senders", headers=_headers(b_admin_token)).json()
    assert listed["total"] == 1
    assert listed["items"][0]["email"] == "zoe@example.com"

    b_headers = _headers(b_admin_token)
    # Every A-owned resource reads as 404 to B (no existence oracle)
    for sender_key in ("alice", "bob", "charlie", "dave", "fiona", "dana", "gina", "hank"):
        sid = seed[sender_key]
        assert client.get(f"/api/v1/senders/{sid}", headers=b_headers).status_code == 404
        assert client.patch(f"/api/v1/senders/{sid}", json={}, headers=b_headers).status_code == 404
        assert client.post(f"/api/v1/senders/{sid}/enable", headers=b_headers).status_code == 404
        assert client.post(f"/api/v1/senders/{sid}/disable", headers=b_headers).status_code == 404
        assert client.post(f"/api/v1/senders/{sid}/restore", headers=b_headers).status_code == 404
        assert client.delete(f"/api/v1/senders/{sid}", headers=b_headers).status_code == 404


def test_cross_tenant_status_filter_is_isolated(sender_client) -> None:
    client, admin_token, _, b_admin_token, *_ = sender_client
    a_removed = client.get("/api/v1/senders", params={"status": "REMOVED"}, headers=_headers(admin_token)).json()
    b_removed = client.get("/api/v1/senders", params={"status": "REMOVED"}, headers=_headers(b_admin_token)).json()
    assert a_removed["total"] == 2
    assert b_removed["total"] == 0


# ===================================================================== #
# 12. Module-level availability entry point (future email engine)
# ===================================================================== #
def test_module_level_availability_entry_point(sender_client) -> None:
    *_, seed, session_factory = sender_client

    with session_factory() as session:
        # bob is enabled + healthy => available
        bob = get_sender_availability(session, seed["tenant_a"], seed["bob"])
        assert bob.available is True
        assert bob.reason is None

        # alice not enabled => unavailable with SENDER_DISABLED
        alice = get_sender_availability(session, seed["tenant_a"], seed["alice"])
        assert alice.available is False
        assert alice.reason == "SENDER_DISABLED"

        # removed sender => SENDER_REMOVED
        gina = get_sender_availability(session, seed["tenant_a"], seed["gina"])
        assert gina.available is False
        assert gina.reason == "SENDER_REMOVED"

        # foreign tenant => hard not-found (no availability oracle)
        with pytest.raises(SenderNotFoundError):
            get_sender_availability(session, seed["tenant_b"], seed["alice"])