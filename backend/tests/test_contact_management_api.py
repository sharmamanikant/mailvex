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


@pytest.fixture()
def api_client(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'contact_api.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    read_permission = Permission(key="contacts.read", description="Read contacts")
    session.add(read_permission)
    session.flush()

    def make_tenant(name: str, email: str, role_name: str = "Admin", grant_read: bool = False) -> dict:
        tenant = Tenant(name=name, slug=f"{name.lower()}-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        user = User(tenant_id=tenant.id, email=email, password_hash=hash_password("correct horse battery staple"), display_name=name, status="ACTIVE")
        role = Role(tenant_id=tenant.id, name=role_name)
        session.add_all([user, role])
        session.flush()
        session.add(UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id))
        if grant_read:
            session.add(RolePermission(role_id=role.id, permission_id=read_permission.id))
        session.flush()
        return {"tenant_id": tenant.id, "user_id": user.id, "email": email}

    tenant_a = make_tenant("Acme", "admin.a@example.com")
    tenant_b = make_tenant("Globex", "admin.b@example.com")
    viewer_a = make_tenant("Acme", "viewer.a@example.com", role_name="Viewer", grant_read=True)
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
    yield client, tenant_a, tenant_b, viewer_a
    app.dependency_overrides.clear()
    engine.dispose()


def _login(client: TestClient, email: str) -> str:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": "correct horse battery staple"})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed(api_client) -> tuple[TestClient, str, dict, dict, dict]:
    client, tenant_a, tenant_b, _ = api_client
    admin_a_token = _login(client, tenant_a["email"])

    contact = client.post(
        "/api/v1/contacts",
        headers=_headers(admin_a_token),
        json={"first_name": "Ada", "last_name": "Lovelace", "email": "ada@example.com", "company": "Analytical Engines", "custom_fields": {}},
    )
    assert contact.status_code == 201, contact.text
    contact_id = contact.json()["id"]

    field = client.post(
        "/api/v1/contact-fields",
        headers=_headers(admin_a_token),
        json={"key": "experience", "label": "Experience", "field_type": "NUMBER"},
    )
    assert field.status_code == 201, field.text
    field_id = field.json()["id"]

    segment = client.post(
        "/api/v1/segments",
        headers=_headers(admin_a_token),
        json={"name": "Managers", "filters": {"match": "all", "conditions": [{"field": "designation", "operator": "eq", "value": "Manager"}]}},
    )
    assert segment.status_code == 201, segment.text
    segment_id = segment.json()["id"]

    contact_list = client.post(
        "/api/v1/contact-lists",
        headers=_headers(admin_a_token),
        json={"name": "Prospects", "description": None},
    )
    assert contact_list.status_code == 201, contact_list.text
    list_id = contact_list.json()["id"]

    tag = client.post("/api/v1/tags", headers=_headers(admin_a_token), json={"name": "VIP"})
    assert tag.status_code == 201, tag.text
    tag_id = tag.json()["id"]

    return client, admin_a_token, {"contact_id": contact_id, "field_id": field_id, "segment_id": segment_id, "list_id": list_id, "tag_id": tag_id}, tenant_a, tenant_b


def test_admin_can_manage_contact_resources(api_client) -> None:
    client, token, ids, _, _ = _seed(api_client)
    get = client.get(f"/api/v1/contacts/{ids['contact_id']}", headers=_headers(token))
    assert get.status_code == 200
    assert get.json()["email"] == "ada@example.com"

    assert client.get("/api/v1/contacts", headers=_headers(token)).json()["total"] == 1
    assert client.get("/api/v1/contact-fields", headers=_headers(token)).status_code == 200
    assert client.get("/api/v1/segments", headers=_headers(token)).status_code == 200
    assert client.get("/api/v1/contact-lists", headers=_headers(token)).status_code == 200
    assert client.get("/api/v1/tags", headers=_headers(token)).status_code == 200

    # Segment evaluation returns the seeded page (contact has no designation here).
    evaluated = client.get(f"/api/v1/segments/{ids['segment_id']}/contacts", headers=_headers(token))
    assert evaluated.status_code == 200
    assert evaluated.json()["total"] == 0

    # List membership endpoints work.
    members = client.post(f"/api/v1/contact-lists/{ids['list_id']}/members", headers=_headers(token), json={"contact_ids": [ids["contact_id"]]})
    assert members.status_code == 200, members.text
    assert members.json() == {"added": 1}
    contacts_in_list = client.get(f"/api/v1/contact-lists/{ids['list_id']}/contacts", headers=_headers(token))
    assert contacts_in_list.status_code == 200
    assert contacts_in_list.json()["total"] == 1

    # Bulk tag + status guard.
    tagged = client.post("/api/v1/contacts/bulk", headers=_headers(token), json={"contact_ids": [ids["contact_id"]], "action": "tag", "target_id": ids["tag_id"]})
    assert tagged.status_code == 200
    assert tagged.json()["affected"] == 1
    blocked = client.post("/api/v1/contacts/bulk", headers=_headers(token), json={"contact_ids": [ids["contact_id"]], "action": "status", "status": "UNSUBSCRIBED"})
    assert blocked.status_code == 409
    ok_status = client.post("/api/v1/contacts/bulk", headers=_headers(token), json={"contact_ids": [ids["contact_id"]], "action": "status", "status": "INACTIVE"})
    assert ok_status.status_code == 200
    assert ok_status.json()["affected"] == 1


def test_cross_tenant_denies_reads_and_writes(api_client) -> None:
    client, _, ids, _, tenant_b = _seed(api_client)
    admin_b_token = _login(client, tenant_b["email"])
    headers_b = _headers(admin_b_token)

    assert client.get(f"/api/v1/contacts/{ids['contact_id']}", headers=headers_b).status_code == 404
    assert client.patch(f"/api/v1/contacts/{ids['contact_id']}", headers=headers_b, json={"company": "Hacked"}).status_code == 404
    assert client.delete(f"/api/v1/contacts/{ids['contact_id']}", headers=headers_b).status_code == 404
    assert client.delete(f"/api/v1/contact-fields/{ids['field_id']}", headers=headers_b).status_code == 404
    assert client.patch(f"/api/v1/segments/{ids['segment_id']}", headers=headers_b, json={"name": "Stolen"}).status_code == 404
    assert client.delete(f"/api/v1/segments/{ids['segment_id']}", headers=headers_b).status_code == 404
    assert client.get(f"/api/v1/segments/{ids['segment_id']}/contacts", headers=headers_b).status_code == 404
    assert client.patch(f"/api/v1/contact-lists/{ids['list_id']}", headers=headers_b, json={"name": "Stolen"}).status_code == 404
    # Tenant B cannot see tenant A's lists/tags/fields;
    assert client.get("/api/v1/contact-lists", headers=headers_b).json() == []
    assert client.get("/api/v1/tags", headers=headers_b).json() == []
    assert client.get("/api/v1/contact-fields", headers=headers_b).json() == []
    assert client.get(f"/api/v1/contact-lists/{ids['list_id']}/contacts", headers=headers_b).status_code == 404


def test_viewer_cannot_modify_contacts(api_client) -> None:
    client = api_client[0]
    viewer_a = api_client[3]
    _, _, ids, _, _ = _seed(api_client)
    viewer_token = _login(client, viewer_a["email"])
    headers_viewer = _headers(viewer_token)

    assert client.get("/api/v1/contacts", headers=headers_viewer).status_code == 200
    assert client.get("/api/v1/contact-fields", headers=headers_viewer).status_code == 200
    assert client.get("/api/v1/segments", headers=headers_viewer).status_code == 200
    assert client.get("/api/v1/contact-lists", headers=headers_viewer).status_code == 200

    assert client.post("/api/v1/contacts", headers=headers_viewer, json={"first_name": "X", "email": "x@example.com"}).status_code == 403
    assert client.patch(f"/api/v1/contacts/{ids['contact_id']}", headers=headers_viewer, json={"company": "Nope"}).status_code == 403
    assert client.post("/api/v1/contact-fields", headers=headers_viewer, json={"key": "x", "label": "X", "field_type": "TEXT"}).status_code == 403
    assert client.post("/api/v1/segments", headers=headers_viewer, json={"name": "S", "filters": {"match": "all", "conditions": [{"field": "status", "operator": "eq", "value": "ACTIVE"}]}}).status_code == 403
    assert client.post("/api/v1/contact-lists", headers=headers_viewer, json={"name": "L"}).status_code == 403
    assert client.post("/api/v1/tags", headers=headers_viewer, json={"name": "T"}).status_code == 403
    assert client.post("/api/v1/contacts/bulk", headers=headers_viewer, json={"contact_ids": [ids["contact_id"]], "action": "tag", "target_id": ids["tag_id"]}).status_code == 403
    assert client.post(f"/api/v1/contact-lists/{ids['list_id']}/members", headers=headers_viewer, json={"contact_ids": [ids["contact_id"]]}).status_code == 403
    assert client.delete(f"/api/v1/contact-lists/{ids['list_id']}/members/{ids['contact_id']}", headers=headers_viewer).status_code == 403


def test_new_field_definitions_validate_contact_payload(api_client) -> None:
    client = api_client[0]
    _, token, _, _, _ = _seed(api_client)
    add_field = client.post("/api/v1/contact-fields", headers=_headers(token), json={"key": "budget", "label": "Budget", "field_type": "NUMBER"})
    assert add_field.status_code == 201
    valid = client.post("/api/v1/contacts", headers=_headers(token), json={"first_name": "Grace", "email": "grace@example.com", "custom_fields": {"budget": "42"}})
    assert valid.status_code == 201
    assert valid.json()["custom_fields"]["budget"] == "42"
    invalid = client.post("/api/v1/contacts", headers=_headers(token), json={"first_name": "Alan", "email": "alan@example.com", "custom_fields": {"budget": "lots"}})
    assert invalid.status_code == 409
    assert client.get("/api/v1/contacts", headers=_headers(token)).json()["total"] == 2