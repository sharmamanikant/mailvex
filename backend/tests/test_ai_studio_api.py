from __future__ import annotations

from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.core.database import get_db
from app.main import app
from app.models import Base, Permission, Role, RolePermission, Tenant, User, UserRole
from app.security.passwords import hash_password

PASSWORD = "correct horse battery staple"

OBJECTIVE = "Introduce IT infrastructure support services to an IT manager"


@pytest.fixture()
def studio_api(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'studio_api.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    read_perm = Permission(key="templates.read", description="Read templates/drafts")
    create_perm = Permission(key="templates.create", description="Create templates/drafts")
    update_perm = Permission(key="templates.update", description="Update templates/drafts")
    session.add_all([read_perm, create_perm, update_perm])
    session.flush()
    permission_lookup = {
        "templates.read": read_perm,
        "templates.create": create_perm,
        "templates.update": update_perm,
    }

    def make_tenant(name: str, email: str, role_name: str = "Admin", perms: tuple[str, ...] = (), tenant_id=None) -> dict:
        if tenant_id is not None:
            tenant = session.get(Tenant, tenant_id)
        else:
            tenant = Tenant(name=name, slug=f"{name.lower()}-{uuid4().hex[:8]}")
            session.add(tenant)
            session.flush()
        user = User(tenant_id=tenant.id, email=email, password_hash=hash_password(PASSWORD), display_name=name, status="ACTIVE")
        role = Role(tenant_id=tenant.id, name=role_name)
        session.add_all([user, role])
        session.flush()
        session.add(UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id))
        for permission_key in perms:
            session.add(RolePermission(role_id=role.id, permission_id=permission_lookup[permission_key].id))
        session.flush()
        return {"tenant_id": tenant.id, "user_id": user.id, "email": email}

    tenant_a = make_tenant("Acme", "a@acme.io")
    tenant_b = make_tenant("Globex", "b@globex.io")
    reader = make_tenant(
        "Reader", "reader@acme.io", role_name="Reader", perms=("templates.read",), tenant_id=tenant_a["tenant_id"]
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
    yield client, session_factory, tenant_a, tenant_b, reader
    app.dependency_overrides.clear()
    engine.dispose()


def login(client: TestClient, email: str) -> str:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": PASSWORD})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def generate(client: TestClient, token: str) -> dict:
    response = client.post(
        "/api/v1/ai/drafts",
        headers=headers(token),
        json={
            "objective": OBJECTIVE,
            "service": "IT infrastructure support",
            "cta": "Would a short call work this week?",
            "tone": "PROFESSIONAL",
            "language": "English",
            "desired_length": "MEDIUM",
            "generation_type": "INITIAL_EMAIL",
            "recipient": {"first_name": "Rohan", "company": "Acme"},
            "sender": {"sender_name": "Maya"},
            "custom_values": {},
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_rbac_read_only_user_cannot_generate_or_review(studio_api) -> None:
    client, _, tenant_a, _, reader = studio_api
    admin = login(client, tenant_a["email"])
    viewer = login(client, reader["email"])

    draft = generate(client, admin)
    draft_id = draft["id"]

    # read-only user cannot generate a draft (requires templates.create)
    response = client.post(
        "/api/v1/ai/drafts",
        headers=headers(viewer),
        json={"objective": OBJECTIVE, "tone": "PROFESSIONAL", "language": "English", "desired_length": "MEDIUM"},
    )
    assert response.status_code == 403

    # read-only user cannot approve (requires templates.create)
    response = client.post(
        f"/api/v1/ai/drafts/{draft_id}/approve",
        headers=headers(viewer),
        json={"note": "nope"},
    )
    assert response.status_code == 403

    # read-only user cannot transform (requires templates.create)
    response = client.post(
        f"/api/v1/ai/drafts/{draft_id}/transform",
        headers=headers(viewer),
        json={"action": "regenerate"},
    )
    assert response.status_code == 403

    # read-only user cannot update (requires templates.update)
    response = client.patch(
        f"/api/v1/ai/drafts/{draft_id}",
        headers=headers(viewer),
        json={"body": "nope"},
    )
    assert response.status_code == 403


def test_read_allowed_for_viewer(studio_api) -> None:
    client, _, tenant_a, _, reader = studio_api
    admin = login(client, tenant_a["email"])
    draft = generate(client, admin)
    response = client.get(
        f"/api/v1/ai/drafts/{draft['id']}", headers=headers(login(client, reader["email"]))
    )
    assert response.status_code == 200
    assert "Rohan" in response.json()["generated_body"]


def test_unauthenticated_is_rejected(studio_api) -> None:
    client, *_ = studio_api
    assert client.get("/api/v1/ai/drafts").status_code == 401
    assert client.get("/api/v1/ai/drafts/usage").status_code == 401
    assert client.post(
        "/api/v1/ai/drafts", json={"objective": OBJECTIVE, "tone": "PROFESSIONAL", "language": "English", "desired_length": "MEDIUM"}
    ).status_code == 401


def test_tenant_isolation_at_api_level(studio_api) -> None:
    client, _, tenant_a, tenant_b, _ = studio_api
    admin = login(client, tenant_a["email"])
    other = login(client, tenant_b["email"])
    draft = generate(client, admin)

    # tenant B cannot read tenant A's draft
    response = client.get(
        f"/api/v1/ai/drafts/{draft['id']}", headers=headers(other)
    )
    assert response.status_code == 404

    # tenant B's list does not include tenant A's draft
    response = client.get("/api/v1/ai/drafts", headers=headers(other))
    assert response.status_code == 200
    assert all(item["id"] != draft["id"] for item in response.json()["items"])


def test_approval_flow_through_api(studio_api) -> None:
    client, _, tenant_a, _, _ = studio_api
    admin = login(client, tenant_a["email"])

    draft = generate(client, admin)
    assert draft["generation_status"] == "GENERATED"

    # cannot save-as-template before approval
    response = client.post(
        f"/api/v1/ai/drafts/{draft['id']}/save-as-template",
        headers=headers(admin),
        json={"template_name": "Premature"},
    )
    assert response.status_code == 400

    # approve
    approved = client.post(
        f"/api/v1/ai/drafts/{draft['id']}/approve",
        headers=headers(admin),
        json={"note": "looks good"},
    )
    assert approved.status_code == 200
    assert approved.json()["generation_status"] == "APPROVED"
    assert approved.json()["approved_by_id"] is not None

    # an approved draft is immutable
    mutate = client.patch(
        f"/api/v1/ai/drafts/{draft['id']}",
        headers=headers(admin),
        json={"body": "edited"},
    )
    assert mutate.status_code == 400

    # now save-as-template succeeds
    saved = client.post(
        f"/api/v1/ai/drafts/{draft['id']}/save-as-template",
        headers=headers(admin),
        json={"template_name": "Approved intro"},
    )
    assert saved.status_code == 200
    assert saved.json()["approved_template_id"] is not None


def test_usage_endpoint_reports_tenant_generations(studio_api) -> None:
    client, _, tenant_a, _, _ = studio_api
    admin = login(client, tenant_a["email"])
    generate(client, admin)
    generate(client, admin)
    response = client.get("/api/v1/ai/drafts/usage", headers=headers(admin))
    assert response.status_code == 200
    usage = response.json()
    assert usage["monthly_generations"] == 2

