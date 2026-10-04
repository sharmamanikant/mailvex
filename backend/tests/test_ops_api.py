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
    User,
    UserRole,
)
from app.security.passwords import hash_password


@pytest.fixture()
def ops_client(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'ops_api.db'}",
        connect_args={"check_same_thread": False, "timeout": 30},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = factory()

    tenant = Tenant(name="Ops", slug=f"ops-{uuid4().hex[:8]}")
    session.add(tenant)
    session.flush()
    admin = User(
        tenant_id=tenant.id,
        email="admin@ops.example.com",
        password_hash=hash_password("correct horse battery staple"),
        display_name="Admin",
        status="ACTIVE",
    )
    viewer = User(
        tenant_id=tenant.id,
        email="viewer@ops.example.com",
        password_hash=hash_password("correct horse battery staple"),
        display_name="Viewer",
        status="ACTIVE",
    )
    super_admin = User(
        tenant_id=tenant.id,
        email="superadmin@ops.example.com",
        password_hash=hash_password("correct horse battery staple"),
        display_name="Super Admin",
        status="ACTIVE",
    )
    admin_role = Role(tenant_id=tenant.id, name="Admin")
    viewer_role = Role(tenant_id=tenant.id, name="Viewer")
    super_admin_role = Role(tenant_id=tenant.id, name="Super Admin")
    session.add_all([admin, viewer, super_admin, admin_role, viewer_role, super_admin_role])
    session.flush()
    session.add_all([
        UserRole(tenant_id=tenant.id, user_id=admin.id, role_id=admin_role.id),
        UserRole(tenant_id=tenant.id, user_id=viewer.id, role_id=viewer_role.id),
        UserRole(tenant_id=tenant.id, user_id=super_admin.id, role_id=super_admin_role.id),
    ])
    permission = Permission(key="settings.manage", description="Manage settings")
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


def test_ops_endpoints_require_super_admin(ops_client) -> None:
    client = ops_client
    token = _login(client, "viewer@ops.example.com")
    headers = {"Authorization": f"Bearer {token}"}
    for path in ("/api/v1/ops/overview", "/api/v1/ops/readiness", "/api/v1/ops/metrics", "/api/v1/ops/alerts"):
        response = client.get(path, headers=headers)
        assert response.status_code == 403, path

    admin_token = _login(client, "admin@ops.example.com")
    response = client.get(
        "/api/v1/ops/overview",
        headers={"Authorization": f"Bearer {admin_token}"},
    )
    assert response.status_code == 403, "tenant admin must not see cross-tenant ops data"


def test_ops_metric_require_auth(ops_client) -> None:
    client = ops_client
    response = client.get("/api/v1/ops/metrics")
    assert response.status_code == 401


def test_ops_overview(ops_client) -> None:
    client = ops_client
    token = _login(client, "superadmin@ops.example.com")
    headers = {"Authorization": f"Bearer {token}"}
    response = client.get("/api/v1/ops/overview", headers=headers)
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "readiness" in payload
    assert "metrics" in payload
    assert "alerts" in payload
    assert payload["ready"] is not None
    assert isinstance(payload["samples"], list)


def test_ops_readiness_and_metrics(ops_client) -> None:
    client = ops_client
    headers = {"Authorization": f"Bearer {_login(client, 'superadmin@ops.example.com')}"}
    readiness = client.get("/api/v1/ops/readiness", headers=headers)
    assert readiness.status_code == 200
    assert set(readiness.json()) >= {"ready", "database", "redis", "worker", "scheduler"}
    metrics = client.get("/api/v1/ops/metrics", headers=headers)
    assert metrics.status_code == 200
    assert "metrics" in metrics.json()


def test_ops_alerts_lifecycle(ops_client) -> None:
    client = ops_client
    headers = {"Authorization": f"Bearer {_login(client, 'superadmin@ops.example.com')}"}

    evaluate = client.post("/api/v1/ops/alerts/evaluate", headers=headers)
    assert evaluate.status_code == 200, evaluate.text
    payload = evaluate.json()
    assert set(payload) >= {"results", "created", "resolved"}

    open_alerts = client.get("/api/v1/ops/alerts/open", headers=headers)
    assert open_alerts.status_code == 200
    alerts = open_alerts.json()["alerts"]
    if alerts:
        alert_id = alerts[0]["id"]
        ack = client.post(f"/api/v1/ops/alerts/{alert_id}/ack", headers=headers)
        assert ack.status_code == 200
        assert ack.json()["alert"]["status"] == "ACKNOWLEDGED"

    recent = client.get("/api/v1/ops/alerts", headers=headers)
    assert recent.status_code == 200
    assert isinstance(recent.json()["alerts"], list)


def test_ops_alerts_ack_unknown_404(ops_client) -> None:
    client = ops_client
    headers = {"Authorization": f"Bearer {_login(client, 'superadmin@ops.example.com')}"}
    response = client.post(f"/api/v1/ops/alerts/{uuid4()}/ack", headers=headers)
    assert response.status_code == 404


def test_ops_sample_and_history(ops_client) -> None:
    client = ops_client
    headers = {"Authorization": f"Bearer {_login(client, 'superadmin@ops.example.com')}"}
    sample = client.post("/api/v1/ops/sample", headers=headers)
    assert sample.status_code == 200, sample.text
    values = sample.json()["values"]
    assert "db.latency" in values
    assert "queue.delivery" in values

    history = client.get("/api/v1/ops/metrics/history?limit=20", headers=headers)
    assert history.status_code == 200
    assert isinstance(history.json()["samples"], list)

    filtered = client.get("/api/v1/ops/metrics/history?metric=db.latency", headers=headers)
    assert filtered.status_code == 200
    assert all(s["metric"] == "db.latency" for s in filtered.json()["samples"])

    bad_limit = client.get("/api/v1/ops/metrics/history?limit=9999", headers=headers)
    assert bad_limit.status_code == 422


def test_health_live_and_ready_are_public_and_minimal(ops_client) -> None:
    client = ops_client
    live = client.get("/health/live")
    assert live.status_code == 200
    assert live.json() == {"status": "alive"}

    ready = client.get("/health/ready")
    assert ready.status_code in {200, 503}
    body = ready.json()
    assert set(body) == {"status"}
    assert body["status"] in {"ready", "not_ready"}


def test_health_details_requires_settings_manage(ops_client) -> None:
    client = ops_client
    assert client.get("/health/details").status_code == 401

    viewer = _login(client, "viewer@ops.example.com")
    assert (
        client.get("/health/details", headers={"Authorization": f"Bearer {viewer}"}).status_code
        == 403
    )

    admin = _login(client, "admin@ops.example.com")
    response = client.get("/health/details", headers={"Authorization": f"Bearer {admin}"})
    assert response.status_code == 200
    assert set(response.json()) >= {"status", "database", "redis", "worker", "scheduler"}