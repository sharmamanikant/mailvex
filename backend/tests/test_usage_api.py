from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import get_db
from app.main import app
from app.models import (
    Base,
    Permission,
    Role,
    RolePermission,
    Tenant,
    TenantSubscription,
    User,
    UserRole,
)
from app.security.passwords import hash_password


@pytest.fixture()
def usage_client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'usage_api.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()

    tenant = Tenant(name="Usage", slug=f"usage-{uuid4().hex[:8]}")
    session.add(tenant)
    session.flush()
    admin = User(
        tenant_id=tenant.id,
        email="admin@usage.example.com",
        password_hash=hash_password("correct horse battery staple"),
        display_name="Admin",
    )
    viewer = User(
        tenant_id=tenant.id,
        email="viewer@usage.example.com",
        password_hash=hash_password("correct horse battery staple"),
        display_name="Viewer",
    )
    admin_role = Role(tenant_id=tenant.id, name="Admin")
    viewer_role = Role(tenant_id=tenant.id, name="Viewer")
    session.add_all([admin, viewer, admin_role, viewer_role])
    session.flush()
    session.add_all([
        TenantSubscription(
            tenant_id=tenant.id,
            plan_code="free",
            status="ACTIVE",
            custom_limits={},
        ),
        UserRole(tenant_id=tenant.id, user_id=admin.id, role_id=admin_role.id),
        UserRole(tenant_id=tenant.id, user_id=viewer.id, role_id=viewer_role.id),
    ])
    for key, description in (
        ("billing.read", "Read usage"),
        ("billing.manage", "Manage billing"),
    ):
        permission = Permission(key=key, description=description)
        session.add(permission)
        session.flush()
        session.add(RolePermission(role_id=admin_role.id, permission_id=permission.id))
    session.commit()
    session.close()

    def override_db():
        db = factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    yield client
    app.dependency_overrides.clear()
    engine.dispose()


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/api/v1/auth/login",
        json={"email": email, "password": "correct horse battery staple"},
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def test_usage_overview_requires_billing_read(usage_client) -> None:
    client = usage_client
    token = _login(client, "viewer@usage.example.com")
    response = client.get(
        "/api/v1/usage/overview",
        headers={"Authorization": f"Bearer {token}"},
    )
    assert response.status_code == 403


def test_usage_read_endpoints(usage_client) -> None:
    client = usage_client
    token = _login(client, "admin@usage.example.com")
    headers = {"Authorization": f"Bearer {token}"}

    overview = client.get("/api/v1/usage/overview", headers=headers)
    assert overview.status_code == 200
    payload = overview.json()
    assert payload["plan_code"] == "free"
    assert payload["plan_name"] == "Free"
    assert {m["metric"] for m in payload["metrics"]} == {
        "contacts",
        "ai_generations",
        "campaigns",
        "messages",
        "connected_senders",
        "storage_mb",
        "team_members",
    }

    plans = client.get("/api/v1/usage/plans", headers=headers)
    assert plans.status_code == 200
    plan_codes = {p["code"] for p in plans.json()}
    assert plan_codes == {"free", "starter", "business", "enterprise"}

    limits = client.get("/api/v1/usage/limits", headers=headers)
    assert limits.status_code == 200
    by_metric = {item["metric"]: item for item in limits.json()}
    assert by_metric["contacts"]["limit"] == 1000
    assert by_metric["contacts"]["scope"] == "absolute"
    assert by_metric["ai_generations"]["scope"] == "period"

    events = client.get("/api/v1/usage/events", headers=headers)
    assert events.status_code == 200
    assert isinstance(events.json(), list)


def test_record_event_and_limit_rejection(usage_client) -> None:
    client = usage_client
    token = _login(client, "admin@usage.example.com")
    headers = {"Authorization": f"Bearer {token}"}

    recorded = client.post(
        "/api/v1/usage/events",
        headers=headers,
        json={"event_type": "AI_GENERATION", "quantity": 1},
    )
    assert recorded.status_code == 201
    assert recorded.json()["event_type"] == "AI_GENERATION"

    unknown = client.post(
        "/api/v1/usage/events",
        headers=headers,
        json={"event_type": "NOT_A_REAL_EVENT"},
    )
    assert unknown.status_code == 400


def test_plan_change_via_api(usage_client) -> None:
    client = usage_client
    token = _login(client, "admin@usage.example.com")
    headers = {"Authorization": f"Bearer {token}"}

    changed = client.patch(
        "/api/v1/usage/plan",
        headers=headers,
        json={"plan_code": "starter", "reset_period": True},
    )
    assert changed.status_code == 200
    assert changed.json()["plan_code"] == "starter"

    overview = client.get("/api/v1/usage/overview", headers=headers).json()
    assert overview["plan_code"] == "starter"

    invalid = client.patch(
        "/api/v1/usage/plan",
        headers=headers,
        json={"plan_code": "platinum"},
    )
    assert invalid.status_code == 400


def test_plan_change_requires_billing_manage(usage_client) -> None:
    client = usage_client
    token = _login(client, "viewer@usage.example.com")
    response = client.patch(
        "/api/v1/usage/plan",
        headers={"Authorization": f"Bearer {token}"},
        json={"plan_code": "starter"},
    )
    assert response.status_code == 403