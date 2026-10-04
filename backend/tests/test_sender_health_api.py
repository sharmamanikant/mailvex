"""API tests for the Phase 5 sender health endpoints.

Covers authentication, RBAC, tenant isolation, deterministic health runs
(injected static DNS resolver), manual-check rate limiting, in-progress
conflicts, secret redaction, the health overview, and paginated history.
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
from app.models import (
    AuditLog,
    Base,
    Mailbox,
    Permission,
    ProviderConnection,
    Role,
    RolePermission,
    Sender,
    SenderHealthCheck,
    SenderHealthCheckResult,
    Tenant,
    User,
    UserRole,
)
from app.security.passwords import hash_password
from app.security.rate_limit import RateLimitResult
from app.security.tokens import TokenService
from app.services.sender_health_engine import SenderHealthService
from app.services.sender_health_engine.dns import StaticDnsResolver

PASSWORD = "correct horse battery staple"

HEALTHY_DNS = StaticDnsResolver(
    txt={
        "example.com": ["v=spf1 include:_spf.google.com ~all"],
        "google._domainkey.example.com": ["v=DKIM1; k=rsa; p=abc"],
        "_dmarc.example.com": ["v=DMARC1; p=reject"],
    },
    mx={"example.com": ["mail.example.com"]},
)


def _mint_token(user_id, tenant_id, roles) -> str:
    token, _ = TokenService(settings).create_access_token(user_id, tenant_id, roles)
    return token


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _use_static_resolver(monkeypatch) -> None:
    """Point the health route builder at a static-DNS service (no live DNS)."""
    import app.api.senders as senders_api

    def _service(session, principal):
        return SenderHealthService(
            session,
            principal.tenant_id,
            principal.user_id,
            resolver=HEALTHY_DNS,
            rate_limiter=senders_api.health_rate_limits,
        )

    monkeypatch.setattr(senders_api, "_health_service", _service)


@pytest.fixture()
def health_client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'health_api.db'}",
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

    admin = User(
        tenant_id=tenant_a.id,
        email="admin@example.com",
        password_hash=hash_password(PASSWORD),
        display_name="Admin",
    )
    member = User(
        tenant_id=tenant_a.id,
        email="member@example.com",
        password_hash=hash_password(PASSWORD),
        display_name="Member",
    )
    b_admin = User(
        tenant_id=tenant_b.id,
        email="badmin@example.com",
        password_hash=hash_password(PASSWORD),
        display_name="Beta Admin",
    )
    session.add_all([admin, member, b_admin])
    session.flush()

    session.add_all(
        [
            UserRole(tenant_id=tenant_a.id, user_id=admin.id, role_id=admin_role.id),
            UserRole(tenant_id=tenant_a.id, user_id=member.id, role_id=member_role.id),
            RolePermission(role_id=admin_role.id, permission_id=connect_perm.id),
            RolePermission(role_id=admin_role.id, permission_id=read_perm.id),
            RolePermission(role_id=member_role.id, permission_id=read_perm.id),
            UserRole(tenant_id=tenant_b.id, user_id=b_admin.id, role_id=b_admin_role.id),
            RolePermission(role_id=b_admin_role.id, permission_id=connect_perm.id),
            RolePermission(role_id=b_admin_role.id, permission_id=read_perm.id),
        ]
    )

    connection = ProviderConnection(
        tenant_id=tenant_a.id,
        provider="GOOGLE",
        connection_type="OAUTH",
        status="CONNECTED",
        provider_account_id="acct-1",
        workspace_domain="example.com",
        credential_reference="encrypted:never-export",
    )
    session.add(connection)
    session.flush()
    mailbox = Mailbox(
        tenant_id=tenant_a.id,
        provider_connection_id=connection.id,
        provider_mailbox_id="m1",
        email="sender@example.com",
        provider_status="ACTIVE",
    )
    session.add(mailbox)
    session.flush()
    sender = Sender(
        tenant_id=tenant_a.id,
        mailbox_id=mailbox.id,
        provider_connection_id=connection.id,
        email="sender@example.com",
        display_name="Sender",
        provider="GOOGLE",
        status="ACTIVE",
        sending_enabled=True,
        health_status="UNKNOWN",
    )
    session.add(sender)
    session.commit()
    sender_id = sender.id

    admin_token = _mint_token(admin.id, tenant_a.id, ["Admin"])
    member_token = _mint_token(member.id, tenant_a.id, ["Member"])
    b_admin_token = _mint_token(b_admin.id, tenant_b.id, ["Admin"])

    def override_get_db():
        yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    yield client, admin_token, member_token, b_admin_token, sender_id, session, session_factory
    app.dependency_overrides.pop(get_db, None)
    session.close()


# ===================================================================== #
# Authentication + RBAC
# ===================================================================== #
def test_health_endpoints_require_auth(health_client) -> None:
    client, *_ = health_client
    sender_id = health_client[4]
    assert client.post(f"/api/v1/senders/{sender_id}/health-check").status_code == 401
    assert client.get(f"/api/v1/senders/{sender_id}/health").status_code == 401
    assert client.get(f"/api/v1/senders/{sender_id}/health/history").status_code == 401


def test_manual_check_requires_integrations_connect(health_client, monkeypatch) -> None:
    client, _, member_token, *_ = health_client
    sender_id = health_client[4]
    _use_static_resolver(monkeypatch)
    response = client.post(
        f"/api/v1/senders/{sender_id}/health-check", headers=_headers(member_token)
    )
    assert response.status_code == 403
    assert b"permission" in response.content.lower() or b"permitted" in response.content.lower()


def test_read_endpoints_allowed_for_member(health_client, monkeypatch) -> None:
    client, _, member_token, *_ = health_client
    sender_id = health_client[4]
    _use_static_resolver(monkeypatch)
    response = client.get(f"/api/v1/senders/{sender_id}/health", headers=_headers(member_token))
    assert response.status_code == 200
    assert response.json()["health_status"] == "UNKNOWN"


# ===================================================================== #
# Running a health check
# ===================================================================== #
def test_run_health_check_success(health_client, monkeypatch) -> None:
    client, admin_token, *_ = health_client
    sender_id = health_client[4]
    _use_static_resolver(monkeypatch)

    response = client.post(
        f"/api/v1/senders/{sender_id}/health-check", headers=_headers(admin_token)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["health_check_id"]
    assert body["sender_id"] == str(sender_id)
    assert body["overall_status"] == "HEALTHY"
    assert float(body["overall_score"]) == 100.0
    assert body["score_version"] == "v1"
    assert body["triggered_by"] == "MANUAL"
    assert len(body["results"]) == 9
    statuses = {r["check_type"]: r["status"] for r in body["results"]}
    assert statuses["SENDING_SIGNALS"] == "UNKNOWN"
    assert body["score_explanation"]["weights"]["PROVIDER_CONNECTION"] == 20.0
    assert body["score_explanation"]["thresholds"] == {"healthy": 80.0, "warning": 60.0}

    assert "credential_reference" not in str(body)
    assert "encrypted" not in str(body)


def test_run_health_check_updates_sender(health_client, monkeypatch) -> None:
    client, admin_token, *_ = health_client
    sender_id = health_client[4]
    session = health_client[5]
    _use_static_resolver(monkeypatch)

    client.post(f"/api/v1/senders/{sender_id}/health-check", headers=_headers(admin_token))
    session.expire_all()
    sender = session.get(Sender, sender_id)
    assert sender.health_status == "HEALTHY"
    assert float(sender.health_score) == 100.0
    assert sender.last_health_check_at is not None


def test_run_health_check_rate_limited(health_client, monkeypatch) -> None:
    client, admin_token, *_ = health_client
    sender_id = health_client[4]
    _use_static_resolver(monkeypatch)
    import app.api.senders as senders_api

    class Denied:
        def check_limit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult:
            return RateLimitResult(False, 45)

    monkeypatch.setattr(senders_api, "health_rate_limits", Denied())
    response = client.post(
        f"/api/v1/senders/{sender_id}/health-check", headers=_headers(admin_token)
    )
    assert response.status_code == 429
    assert response.json()["detail"]["code"] == "HEALTH_CHECK_RATE_LIMITED"


def test_run_health_check_in_progress_conflict(health_client, monkeypatch) -> None:
    client, admin_token, *_ = health_client
    sender_id = health_client[4]
    session = health_client[5]
    _use_static_resolver(monkeypatch)
    from sqlalchemy import update

    session.execute(update(Sender).where(Sender.id == sender_id).values(health_status="CHECKING"))
    session.commit()
    response = client.post(
        f"/api/v1/senders/{sender_id}/health-check", headers=_headers(admin_token)
    )
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "HEALTH_CHECK_IN_PROGRESS"


# ===================================================================== #
# Tenant isolation
# ===================================================================== #
def test_health_cross_tenant_is_forbidden(health_client, monkeypatch) -> None:
    client, _, _, b_admin_token, *_ = health_client
    sender_id = health_client[4]
    _use_static_resolver(monkeypatch)
    assert (
        client.post(f"/api/v1/senders/{sender_id}/health-check", headers=_headers(b_admin_token)).status_code
        == 404
    )
    assert client.get(f"/api/v1/senders/{sender_id}/health", headers=_headers(b_admin_token)).status_code == 404


def test_run_health_check_missing_sender_404(health_client, monkeypatch) -> None:
    client, admin_token, *_ = health_client
    _use_static_resolver(monkeypatch)
    response = client.post(
        f"/api/v1/senders/{uuid4()}/health-check", headers=_headers(admin_token)
    )
    assert response.status_code == 404


# ===================================================================== #
# Overview + history
# ===================================================================== #
def test_health_overview_and_audit_trail(health_client, monkeypatch) -> None:
    client, admin_token, *_ = health_client
    sender_id = health_client[4]
    session = health_client[5]
    _use_static_resolver(monkeypatch)

    client.post(f"/api/v1/senders/{sender_id}/health-check", headers=_headers(admin_token))
    response = client.get(f"/api/v1/senders/{sender_id}/health", headers=_headers(admin_token))
    assert response.status_code == 200
    body = response.json()
    assert body["id"] == str(sender_id)
    assert body["email"] == "sender@example.com"
    assert body["provider"] == "GOOGLE"
    assert body["health_status"] == "HEALTHY"
    assert body["latest"]["overall_status"] == "HEALTHY"
    assert len(body["domain_authentication_summary"]) == 4
    domain_types = {r["check_type"] for r in body["domain_authentication_summary"]}
    assert domain_types == {"SPF", "DKIM", "DMARC", "DNS"}

    sender = session.get(Sender, sender_id)
    actions = session.query(AuditLog).filter(AuditLog.tenant_id == sender.tenant_id).count()
    assert actions >= 2


def test_health_overview_before_any_check(health_client) -> None:
    client, admin_token, *_ = health_client
    sender_id = health_client[4]
    response = client.get(f"/api/v1/senders/{sender_id}/health", headers=_headers(admin_token))
    assert response.status_code == 200
    body = response.json()
    assert body["latest"] is None
    assert body["summary"] is None
    assert body["health_status"] == "UNKNOWN"


def test_health_history_pagination_and_counts(health_client, monkeypatch) -> None:
    client, admin_token, *_ = health_client
    sender_id = health_client[4]
    session = health_client[5]
    _use_static_resolver(monkeypatch)

    client.post(f"/api/v1/senders/{sender_id}/health-check", headers=_headers(admin_token))
    # Seed a second historical run directly (manual runs are rate-limited).
    first = session.query(SenderHealthCheck).filter(
        SenderHealthCheck.sender_id == sender_id
    ).first()
    second = SenderHealthCheck(
        tenant_id=first.tenant_id,
        sender_id=sender_id,
        overall_status="WARNING",
        score_version="v1",
        triggered_by="SCHEDULED",
    )
    session.add(second)
    session.flush()
    session.add(
        SenderHealthCheckResult(
            tenant_id=first.tenant_id,
            health_check_id=second.id,
            check_type="SPF",
            status="FAIL",
            severity="HIGH",
            title="No SPF record published",
        )
    )
    session.commit()

    response = client.get(
        f"/api/v1/senders/{sender_id}/health/history?page=1&page_size=1",
        headers=_headers(admin_token),
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 2
    assert body["total_pages"] == 2
    assert len(body["items"]) == 1
    assert body["items"][0]["health_check_id"] == str(second.id)  # newest first
    assert body["items"][0]["result_count"] == 1

    page2 = client.get(
        f"/api/v1/senders/{sender_id}/health/history?page=2&page_size=1",
        headers=_headers(admin_token),
    )
    assert page2.json()["items"][0]["health_check_id"] == str(first.id)
    assert page2.json()["items"][0]["result_count"] == 9

    serialized = str(response.json()) + str(page2.json())
    for forbidden in ("credential_reference", "encrypted", "access_token", "refresh_token"):
        assert forbidden not in serialized


def test_health_history_rejects_bad_pagination(health_client) -> None:
    client, admin_token, *_ = health_client
    sender_id = health_client[4]
    assert (
        client.get(
            f"/api/v1/senders/{sender_id}/health/history?page=0", headers=_headers(admin_token)
        ).status_code
        == 422
    )
    assert (
        client.get(
            f"/api/v1/senders/{sender_id}/health/history?page_size=9999",
            headers=_headers(admin_token),
        ).status_code
        == 422
    )