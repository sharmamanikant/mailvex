from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, select
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.core.database import get_db
from app.main import app
from app.models import Base, Permission, Role, RolePermission, Tenant, User, UserRole
from app.security.passwords import hash_password
from app.security.tokens import TokenService


@pytest.fixture()
def auth_client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'auth.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()
    tenant = Tenant(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
    session.add(tenant)
    session.flush()
    user = User(tenant_id=tenant.id, email="owner@example.com", password_hash=hash_password("correct horse battery staple"), display_name="Owner")
    role = Role(tenant_id=tenant.id, name="Admin")
    permission = Permission(key="analytics.read", description="Read analytics")
    session.add_all([user, role, permission])
    session.flush()
    session.add_all([UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id), RolePermission(role_id=role.id, permission_id=permission.id)])
    session.commit()
    session.close()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    yield client, tenant.id, user.id
    app.dependency_overrides.clear()
    engine.dispose()


def test_login_me_refresh_and_logout(auth_client) -> None:
    client, tenant_id, user_id = auth_client
    response = client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "correct horse battery staple"})
    assert response.status_code == 200
    assert response.json()["access_token"]
    assert "HttpOnly" in response.headers["set-cookie"]
    assert response.cookies.get("crcrm_refresh")

    token = response.json()["access_token"]
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200
    assert me.json()["tenant_id"] == str(tenant_id)
    assert me.json()["id"] == str(user_id)

    refreshed = client.post("/api/v1/auth/refresh")
    assert refreshed.status_code == 200
    assert refreshed.json()["access_token"] != token
    assert client.post("/api/v1/auth/logout").status_code == 204


@pytest.fixture()
def empty_client(tmp_path):
    """A client whose database has no users at all, as on a fresh install."""
    engine = create_engine(f"sqlite:///{tmp_path / 'empty.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    yield client, session_factory
    app.dependency_overrides.clear()
    engine.dispose()


def test_first_signup_receives_super_admin(empty_client) -> None:
    client, session_factory = empty_client
    response = client.post("/api/v1/auth/signup", json={"display_name": "Owner", "email": "founder@example.com", "password": "correct horse battery staple"})
    assert response.status_code == 201
    token = response.json()["access_token"]

    session = session_factory()
    try:
        roles = set(session.scalars(select(Role.name).join(UserRole, UserRole.role_id == Role.id)).all())
    finally:
        session.close()
    assert "SUPER_ADMIN" in roles
    assert "Admin" in roles

    # The grant has to be usable, not merely present in the roles table.
    overview = client.get("/api/v1/platform-admin/overview", headers={"Authorization": f"Bearer {token}"})
    assert overview.status_code == 200, overview.text


def test_second_signup_is_not_super_admin(empty_client) -> None:
    client, _ = empty_client
    assert client.post("/api/v1/auth/signup", json={"display_name": "First", "email": "first@example.com", "password": "correct horse battery staple"}).status_code == 201
    second = client.post("/api/v1/auth/signup", json={"display_name": "Second", "email": "second@example.com", "password": "correct horse battery staple"})
    assert second.status_code == 201

    token = second.json()["access_token"]
    assert client.get("/api/v1/platform-admin/overview", headers={"Authorization": f"Bearer {token}"}).status_code == 403


def test_signup_creates_account_and_allows_login(auth_client) -> None:
    client, _, _ = auth_client
    response = client.post("/api/v1/auth/signup", json={"display_name": "New Admin", "email": "newadmin@example.com", "password": "correct horse battery staple"})
    assert response.status_code == 201
    payload = response.json()
    assert payload["access_token"]
    assert payload["user"]["email"] == "newadmin@example.com"

    login = client.post("/api/v1/auth/login", json={"email": "newadmin@example.com", "password": "correct horse battery staple"})
    assert login.status_code == 200
    assert login.json()["access_token"]


def test_invalid_login_is_generic(auth_client) -> None:
    client, _, _ = auth_client
    response = client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "wrong password"})
    assert response.status_code == 401
    assert response.json()["detail"] == "Invalid email or password"


def test_unauthorized_api_access_is_rejected(auth_client) -> None:
    client, _, _ = auth_client
    assert client.get("/api/v1/auth/me").status_code == 401
    assert client.get("/api/v1/dashboard").status_code == 401


def test_dev_default_allowed_origins_include_vite_fallback(monkeypatch) -> None:
    monkeypatch.delenv("ALLOWED_ORIGINS", raising=False)
    monkeypatch.delenv("ALLOWED_HOSTS", raising=False)
    settings = Settings.from_env()
    assert "http://localhost:5174" in settings.allowed_origins
    assert "http://127.0.0.1:5174" in settings.allowed_origins


def test_role_permission_allows_protected_endpoint(auth_client) -> None:
    client, _, _ = auth_client
    login = client.post("/api/v1/auth/login", json={"email": "owner@example.com", "password": "correct horse battery staple"})
    response = client.get("/api/v1/dashboard", headers={"Authorization": f"Bearer {login.json()['access_token']}"})
    assert response.status_code == 200


def test_expired_token_is_rejected(auth_client) -> None:
    client, tenant_id, user_id = auth_client
    expired_settings = Settings(app_env="test", database_url="sqlite://", jwt_secret="t" * 32, access_token_minutes=-1)
    token, _ = TokenService(expired_settings).create_access_token(user_id, tenant_id, ["Admin"])
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


def test_cross_tenant_token_is_rejected(auth_client) -> None:
    client, _, user_id = auth_client
    forged_tenant = uuid4()
    token, _ = TokenService(Settings(app_env="test", database_url="sqlite://", jwt_secret="development-only-change-me-32-bytes")).create_access_token(user_id, forged_tenant, ["Admin"])
    response = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert response.status_code == 401


@pytest.fixture()
def member_client(tmp_path):
    """A client with a non-privileged Member role that has explicit grants.

    The ``Admin`` user in ``auth_client`` short-circuits permission resolution,
    so permission-aware behaviour needs a caller who is *not* privileged.
    """
    engine = create_engine(f"sqlite:///{tmp_path / 'member.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()
    tenant = Tenant(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
    other_tenant = Tenant(name="Other", slug=f"other-{uuid4().hex[:8]}")
    session.add_all([tenant, other_tenant])
    session.flush()
    user = User(tenant_id=tenant.id, email="member@example.com", password_hash=hash_password("correct horse battery staple"), display_name="Member")
    role = Role(tenant_id=tenant.id, name="Member")
    granted = Permission(key="contacts.read", description="Read contacts")
    other = Permission(key="settings.manage", description="Manage settings")
    # A second tenant that grants the same user a privileged role. It must not
    # influence what this tenant reports or allows.
    foreign_role = Role(tenant_id=other_tenant.id, name="ForeignAdmin")
    session.add_all([user, role, granted, other, foreign_role])
    session.flush()
    session.add_all(
        [
            UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id),
            RolePermission(role_id=role.id, permission_id=granted.id),
            UserRole(tenant_id=other_tenant.id, user_id=user.id, role_id=foreign_role.id),
            RolePermission(role_id=foreign_role.id, permission_id=other.id),
        ]
    )
    session.commit()
    session.close()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    yield client, tenant.id, user.id
    app.dependency_overrides.clear()
    engine.dispose()


def _bearer(client, email: str) -> str:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": "correct horse battery staple"})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def test_me_reports_granted_permissions_for_non_privileged_roles(member_client) -> None:
    client, tenant_id, user_id = member_client
    token = _bearer(client, "member@example.com")
    me = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})

    assert me.status_code == 200
    body = me.json()
    assert body["id"] == str(user_id)
    assert body["tenant_id"] == str(tenant_id)
    assert body["permissions"] == ["contacts.read"]


def test_reported_permissions_match_what_the_api_actually_allows(member_client) -> None:
    """The client uses this list to hide navigation, so it must not over-report."""
    client, _, _ = member_client
    token = _bearer(client, "member@example.com")
    headers = {"Authorization": f"Bearer {token}"}
    reported = set(client.get("/api/v1/auth/me", headers=headers).json()["permissions"])

    # contacts.read is granted and usable.
    assert "contacts.read" in reported
    assert client.get("/api/v1/contacts", headers=headers).status_code == 200

    # settings.manage is not granted, so it must be absent and stay forbidden.
    assert "settings.manage" not in reported
    assert client.get("/api/v1/admin/permissions", headers=headers).status_code == 403


def test_privileged_roles_receive_the_wildcard_grant(auth_client) -> None:
    client, _, _ = auth_client
    token = _bearer(client, "owner@example.com")
    body = client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"}).json()

    assert body["roles"] == ["Admin"]
    assert body["permissions"] == ["*"]


def test_permissions_do_not_leak_across_tenants(member_client) -> None:
    """A grant held through another tenant's role must never be reported."""
    client, _, _ = member_client
    token = _bearer(client, "member@example.com")
    headers = {"Authorization": f"Bearer {token}"}

    assert "settings.manage" not in client.get("/api/v1/auth/me", headers=headers).json()["permissions"]
    assert client.get("/api/v1/admin/permissions", headers=headers).status_code == 403
