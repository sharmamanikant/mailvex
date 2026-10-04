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
    Contact,
    Permission,
    Role,
    RolePermission,
    SenderProfile,
    Tenant,
    User,
    UserRole,
)
from app.security.passwords import hash_password

PASSWORD = "correct horse battery staple"


@pytest.fixture()
def template_api(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'templates_api.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    read_perm = Permission(key="templates.read", description="Read templates")
    create_perm = Permission(key="templates.create", description="Create templates")
    update_perm = Permission(key="templates.update", description="Update templates")
    contact_read = Permission(key="contacts.read", description="Read contacts")
    session.add_all([read_perm, create_perm, update_perm, contact_read])
    session.flush()
    permission_lookup = {
        "templates.read": read_perm,
        "templates.create": create_perm,
        "templates.update": update_perm,
        "contacts.read": contact_read,
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
    reader = make_tenant("Reader", "reader@acme.io", role_name="Reader", perms=("templates.read", "contacts.read"), tenant_id=tenant_a["tenant_id"])
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


def make_template(client: TestClient, token: str, name: str = "Intro") -> dict:
    response = client.post(
        "/api/v1/templates",
        headers=headers(token),
        json={
            "name": name,
            "description": "Test template",
            "subject_template": "Hello {{first_name}}",
            "html_body": "<p>Hello {{first_name}} at {{company}}</p><script>bad()</script>",
            "custom_variables": [],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()


def test_rbac_read_only_user_cannot_create_or_update(template_api) -> None:
    client, _, tenant_a, _, reader = template_api
    admin = login(client, tenant_a["email"])
    viewer = login(client, reader["email"])

    created = make_template(client, admin)
    template_id = created["template"]["id"]

    response = client.post(
        "/api/v1/templates",
        headers=headers(viewer),
        json={"name": "No", "subject_template": "Hi", "html_body": "<p>Hi</p>"},
    )
    assert response.status_code == 403

    response = client.patch(
        f"/api/v1/templates/{template_id}",
        headers=headers(viewer),
        json={"html_body": "<p>Nope</p>"},
    )
    assert response.status_code == 403

    response = client.post(
        f"/api/v1/templates/{template_id}/status?status=ACTIVE",
        headers=headers(viewer),
    )
    assert response.status_code == 403


def test_rbac_read_allowed_for_viewer(template_api) -> None:
    client, _, tenant_a, _, reader = template_api
    created = make_template(client, login(client, tenant_a["email"]))
    template_id = created["template"]["id"]
    response = client.get(
        f"/api/v1/templates/{template_id}", headers=headers(login(client, reader["email"]))
    )
    assert response.status_code == 200


def test_unauthenticated_is_rejected(template_api) -> None:
    client, *_ = template_api
    assert client.get("/api/v1/templates").status_code == 401
    assert client.get("/api/v1/templates/variables").status_code == 401


def test_full_crud_cycle_and_versions(template_api) -> None:
    client, _, tenant_a, *_ = template_api
    token = login(client, tenant_a["email"])
    created = make_template(client, token)

    template_id = created["template"]["id"]
    assert created["version"]["version_number"] == 1

    updated = client.patch(
        f"/api/v1/templates/{template_id}",
        headers=headers(token),
        json={"subject_template": "Hi {{first_name}} {{last_name}}"},
    )
    assert updated.status_code == 200, updated.text
    assert updated.json()["version"]["version_number"] == 2

    versions = client.get(f"/api/v1/templates/{template_id}/versions", headers=headers(token))
    assert versions.status_code == 200
    assert versions.json()["total"] == 2
    assert versions.json()["items"][0]["subject_template"] == "Hello {{first_name}}"
    assert versions.json()["items"][1]["subject_template"] == "Hi {{first_name}} {{last_name}}"

    version = client.get(
        f"/api/v1/templates/{template_id}/versions/1", headers=headers(token)
    )
    assert version.status_code == 200
    assert version.json()["subject_template"] == "Hello {{first_name}}"


def test_stored_html_is_sanitized(template_api) -> None:
    client, _, tenant_a, *_ = template_api
    created = make_template(client, login(client, tenant_a["email"]))
    assert "<script>" not in created["version"]["html_body"]


def test_preview_and_missing_warnings(template_api) -> None:
    client, _, tenant_a, *_ = template_api
    token = login(client, tenant_a["email"])
    template_id = make_template(client, token)["template"]["id"]
    response = client.post(
        f"/api/v1/templates/{template_id}/preview",
        headers=headers(token),
        json={"recipient": {"first_name": "Rajesh"}, "sender": {}},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert payload["subject"] == "Hello Rajesh"
    assert "company" in payload["missing_variables"]
    assert payload["warnings"]


def test_variables_registry_endpoint(template_api) -> None:
    client, _, tenant_a, *_ = template_api
    response = client.get("/api/v1/templates/variables", headers=headers(login(client, tenant_a["email"])))
    assert response.status_code == 200
    registry = response.json()
    assert "email" in registry["recipient"]
    assert "sender_email" in registry["sender"]


def test_recipient_specific_preview(template_api) -> None:
    client, session_factory, tenant_a, *_ = template_api
    token = login(client, tenant_a["email"])
    template = make_template(
        client,
        token,
        name="CustomPreview",
    )
    template_id = template["template"]["id"]

    session = session_factory()
    contact = Contact(
        tenant_id=tenant_a["tenant_id"],
        first_name="Priya",
        last_name="Nair",
        email="priya@acme.io",
        company="Acme",
        status="ACTIVE",
    )
    session.add(contact)
    session.commit()
    contact_id = str(contact.id)
    session.close()

    response = client.post(
        f"/api/v1/templates/{template_id}/preview/contact/{contact_id}",
        headers=headers(token),
        json={"sender": {}},
    )
    assert response.status_code == 200, response.text
    payload = response.json()
    assert "Priya" in payload["html_body"]
    assert payload["missing_variables"] == []


def test_cross_tenant_isolation_404(template_api) -> None:
    client, _, tenant_a, tenant_b, *_ = template_api
    token_a = login(client, tenant_a["email"])
    token_b = login(client, tenant_b["email"])
    template_id = make_template(client, token_a)["template"]["id"]
    assert client.get(f"/api/v1/templates/{template_id}", headers=headers(token_b)).status_code == 404
    assert client.patch(
        f"/api/v1/templates/{template_id}", headers=headers(token_b), json={"name": "x"}
    ).status_code == 404


def test_status_transitions(template_api) -> None:
    client, _, tenant_a, *_ = template_api
    token = login(client, tenant_a["email"])
    template_id = make_template(client, token)["template"]["id"]
    response = client.post(
        f"/api/v1/templates/{template_id}/status?status=ACTIVE", headers=headers(token)
    )
    assert response.status_code == 200
    assert response.json()["template"]["status"] == "ACTIVE"


def test_ai_draft_workflow_via_api(template_api) -> None:
    client, _, tenant_a, *_ = template_api
    token = login(client, tenant_a["email"])
    created = client.post(
        "/api/v1/ai/drafts",
        headers=headers(token),
        json={
            "objective": "Discuss services",
            "recipient": {"first_name": "Rajesh", "company": "ABC"},
            "sender": {"sender_name": "Manikant"},
            "service": "Python development",
        },
    )
    assert created.status_code == 201, created.text
    draft_id = created.json()["id"]
    assert created.json()["generation_method"] == "PROVIDER"
    assert created.json()["generation_status"] == "GENERATED"
    assert created.json()["warnings"] == []

    approved = client.post(
        f"/api/v1/ai/drafts/{draft_id}/approve",
        headers=headers(token),
        json={"note": "Approved"},
    )
    assert approved.status_code == 200
    assert approved.json()["generation_status"] == "APPROVED"
    assert approved.json()["approved_template_id"] is None

    saved = client.post(
        f"/api/v1/ai/drafts/{draft_id}/save-as-template",
        headers=headers(token),
        json={"template_name": "Approved Outreach"},
    )
    assert saved.status_code == 200
    assert saved.json()["approved_template_id"] is not None

    assert client.get(f"/api/v1/ai/drafts/{draft_id}", headers=headers(token)).json()["generation_status"] == "APPROVED"
    listed = client.get("/api/v1/ai/drafts", headers=headers(token))
    assert listed.status_code == 200
    assert listed.json()["total"] == 1


def test_ai_draft_reject_via_api(template_api) -> None:
    client, _, tenant_a, *_ = template_api
    token = login(client, tenant_a["email"])
    created = client.post(
        "/api/v1/ai/drafts",
        headers=headers(token),
        json={"objective": "Reject me", "recipient": {"first_name": "R"}, "sender": {}},
    )
    draft_id = created.json()["id"]
    rejected = client.post(
        f"/api/v1/ai/drafts/{draft_id}/reject",
        headers=headers(token),
        json={"note": "Not on brand"},
    )
    assert rejected.status_code == 200
    assert rejected.json()["generation_status"] == "REJECTED"
    assert rejected.json()["review_note"] == "Not on brand"


def test_ai_draft_sender_profile_resolution(template_api) -> None:
    client, session_factory, tenant_a, *_ = template_api
    token = login(client, tenant_a["email"])

    from app.models import EmailAccount

    session = session_factory()
    account = EmailAccount(tenant_id=tenant_a["tenant_id"], provider="smtp", email="maya@acme.io", display_name="Maya Rao")
    session.add(account)
    session.flush()
    session.add(SenderProfile(tenant_id=tenant_a["tenant_id"], email_account_id=account.id, company="Acme"))
    session.commit()
    account_id = str(account.id)
    session.close()

    created = client.post(
        "/api/v1/ai/drafts",
        headers=headers(token),
        json={
            "objective": "Discuss services",
            "recipient": {"first_name": "Rajesh"},
            "sender_id": account_id,
        },
    )
    assert created.status_code == 201, created.text
    sender = created.json()["input_context"]["sender"]
    assert sender["sender_email"] == "maya@acme.io"
    assert sender["sender_name"] == "Maya Rao"


def test_ai_draft_rbac_read_only(template_api) -> None:
    client, _, tenant_a, _, reader = template_api
    admin = login(client, tenant_a["email"])
    viewer = login(client, reader["email"])
    created = client.post(
        "/api/v1/ai/drafts",
        headers=headers(admin),
        json={"objective": "x", "recipient": {"first_name": "R"}, "sender": {}},
    )
    draft_id = created.json()["id"]
    assert client.get(f"/api/v1/ai/drafts/{draft_id}", headers=headers(viewer)).status_code == 200
    response = client.post(
        f"/api/v1/ai/drafts/{draft_id}/transform",
        headers=headers(viewer),
        json={"action": "regenerate"},
    )
    assert response.status_code == 403