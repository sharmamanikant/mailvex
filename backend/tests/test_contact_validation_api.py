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
    Tenant,
    User,
    UserRole,
)
from app.security.passwords import hash_password

PASSWORD = "correct horse battery staple"


@pytest.fixture()
def api_client(tmp_path, monkeypatch):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'validation_api.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    read_permission = Permission(key="contacts.read", description="Read contacts")
    create_permission = Permission(key="contacts.create", description="Write contacts")
    session.add_all([read_permission, create_permission])
    session.flush()

    def make_tenant(name: str, email: str, grant: list[Permission]) -> dict:
        tenant = Tenant(name=name, slug=f"{name.lower()}-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        user = User(
            tenant_id=tenant.id,
            email=email,
            password_hash=hash_password(PASSWORD),
            display_name=name,
            status="ACTIVE",
        )
        role = Role(tenant_id=tenant.id, name="Ops")
        session.add_all([user, role])
        session.flush()
        session.add(UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id))
        for permission in grant:
            session.add(
                RolePermission(role_id=role.id, permission_id=permission.id)
            )
        session.flush()
        return {"tenant_id": tenant.id, "user_id": user.id, "email": email}

    tenant_a = make_tenant("Acme", "admin.a@example.com", [read_permission, create_permission])
    tenant_b = make_tenant("Globex", "admin.b@example.com", [read_permission, create_permission])
    reader_a = make_tenant("Acme", "reader.a@example.com", [read_permission])
    session.commit()
    session.close()

    # The API layer must only enqueue: no broker is contacted from a request.
    from app.tasks import scheduler as scheduler_module

    class _Task:
        def __init__(self) -> None:
            self.id = f"task-{uuid4().hex[:8]}"

    monkeypatch.setattr(
        scheduler_module.verify_contact, "delay", lambda *a: _Task()
    )
    monkeypatch.setattr(
        scheduler_module.run_verification_job, "delay", lambda *a: _Task()
    )

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    client = TestClient(app)
    yield client, tenant_a, tenant_b, reader_a, session_factory
    app.dependency_overrides.clear()
    engine.dispose()


def _login(client: TestClient, email: str) -> str:
    response = client.post(
        "/api/v1/auth/login", json={"email": email, "password": PASSWORD}
    )
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _seed_contacts(session_factory, tenant_id: int, count: int = 3) -> list[Contact]:
    session = session_factory()
    contacts = [
        Contact(
            tenant_id=tenant_id,
            email=f"user{index}@acme.example",
            first_name="Ada",
            last_name="Lovelace",
            source="test",
        )
        for index in range(count)
    ]
    session.add_all(contacts)
    session.commit()
    result = [(item.id, item.email) for item in contacts]
    session.close()
    return result


def test_contact_list_exposes_validation_fields(api_client) -> None:
    client, tenant_a, _b, _reader, session_factory = api_client
    _seed_contacts(session_factory, tenant_a["tenant_id"])
    token = _login(client, tenant_a["email"])
    response = client.get("/api/v1/contacts", headers=_headers(token))
    assert response.status_code == 200, response.text
    item = response.json()["items"][0]
    for field in (
        "email_status",
        "email_type",
        "domain_status",
        "mx_status",
        "smtp_status",
        "phone_status",
        "duplicate_status",
        "risk_level",
        "verification_status",
        "verification_score",
    ):
        assert field in item, f"missing {field}"


def test_bulk_verify_creates_a_job(api_client) -> None:
    client, tenant_a, _b, _reader, session_factory = api_client
    seeded = _seed_contacts(session_factory, tenant_a["tenant_id"])
    token = _login(client, tenant_a["email"])
    response = client.post(
        "/api/v1/contacts/bulk-verify",
        headers=_headers(token),
        json={"contact_ids": [str(cid) for cid, _ in seeded]},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "QUEUED"
    assert body["total_count"] == len(seeded)
    assert body["scope"] == "SELECTION"


def test_bulk_verify_requires_a_target(api_client) -> None:
    client, tenant_a, _b, _reader, _sf = api_client
    token = _login(client, tenant_a["email"])
    response = client.post(
        "/api/v1/contacts/bulk-verify", headers=_headers(token), json={}
    )
    assert response.status_code == 422


def test_bulk_verify_accepts_an_explicit_all_contacts_scope(api_client) -> None:
    """The list page's "Verify all" must be a supported request, not a 422."""
    client, tenant_a, _b, _reader, session_factory = api_client
    seeded = _seed_contacts(session_factory, tenant_a["tenant_id"])
    token = _login(client, tenant_a["email"])
    response = client.post(
        "/api/v1/contacts/bulk-verify",
        headers=_headers(token),
        json={"all_contacts": True},
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "QUEUED"
    assert body["scope"] == "TENANT"
    assert body["total_count"] == len(seeded)


def test_bulk_verify_rejects_an_oversized_selection(api_client) -> None:
    client, tenant_a, _b, _reader, _sf = api_client
    token = _login(client, tenant_a["email"])
    response = client.post(
        "/api/v1/contacts/bulk-verify",
        headers=_headers(token),
        json={"contact_ids": [str(uuid4()) for _ in range(5001)]},
    )
    assert response.status_code in (400, 422)


def test_single_verify_is_accepted_and_queued(api_client) -> None:
    client, tenant_a, _b, _reader, session_factory = api_client
    seeded = _seed_contacts(session_factory, tenant_a["tenant_id"], count=1)
    token = _login(client, tenant_a["email"])
    response = client.post(
        f"/api/v1/contacts/{seeded[0][0]}/verify", headers=_headers(token), json={}
    )
    assert response.status_code == 202, response.text
    body = response.json()
    assert body["status"] == "QUEUED"
    assert body["probe_smtp"] is False


def test_verification_job_is_tenant_scoped(api_client) -> None:
    client, tenant_a, tenant_b, _reader, session_factory = api_client
    seeded = _seed_contacts(session_factory, tenant_a["tenant_id"])
    token_a = _login(client, tenant_a["email"])
    job = client.post(
        "/api/v1/contacts/bulk-verify",
        headers=_headers(token_a),
        json={"contact_ids": [str(cid) for cid, _ in seeded]},
    ).json()

    token_b = _login(client, tenant_b["email"])
    response = client.get(
        f"/api/v1/contacts/verification-jobs/{job['id']}", headers=_headers(token_b)
    )
    assert response.status_code == 404


def test_verification_job_is_readable_by_its_owner(api_client) -> None:
    client, tenant_a, _b, _reader, session_factory = api_client
    seeded = _seed_contacts(session_factory, tenant_a["tenant_id"])
    token = _login(client, tenant_a["email"])
    job = client.post(
        "/api/v1/contacts/bulk-verify",
        headers=_headers(token),
        json={"contact_ids": [str(cid) for cid, _ in seeded]},
    ).json()
    response = client.get(
        f"/api/v1/contacts/verification-jobs/{job['id']}", headers=_headers(token)
    )
    assert response.status_code == 200
    assert response.json()["id"] == job["id"]


def test_verification_job_can_be_cancelled(api_client) -> None:
    client, tenant_a, _b, _reader, session_factory = api_client
    seeded = _seed_contacts(session_factory, tenant_a["tenant_id"])
    token = _login(client, tenant_a["email"])
    job = client.post(
        "/api/v1/contacts/bulk-verify",
        headers=_headers(token),
        json={"contact_ids": [str(cid) for cid, _ in seeded]},
    ).json()
    response = client.post(
        f"/api/v1/contacts/verification-jobs/{job['id']}/cancel", headers=_headers(token)
    )
    assert response.status_code == 200
    assert response.json()["status"] == "CANCELLED"


def test_verification_summary_aggregates_by_status(api_client) -> None:
    client, tenant_a, _b, _reader, session_factory = api_client
    seeded = _seed_contacts(session_factory, tenant_a["tenant_id"], count=3)
    session = session_factory()
    contact = session.get(Contact, seeded[0][0])
    contact.verification_status = "LIKELY_VALID"
    contact.risk_level = "LOW"
    session.commit()
    session.close()

    token = _login(client, tenant_a["email"])
    response = client.get("/api/v1/contacts/verification-summary", headers=_headers(token))
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 3
    assert body["by_status"]["LIKELY_VALID"] == 1
    assert body["by_status"]["UNKNOWN"] == 2
    assert body["by_risk"]["LOW"] == 1


def test_summary_does_not_leak_other_tenants(api_client) -> None:
    client, _a, tenant_b, _reader, session_factory = api_client
    _seed_contacts(session_factory, tenant_b["tenant_id"], count=2)
    token = _login(client, tenant_b["email"])
    body = client.get("/api/v1/contacts/verification-summary", headers=_headers(token)).json()
    assert body["total"] == 2


def test_contacts_can_be_filtered_by_verification_status(api_client) -> None:
    client, tenant_a, _b, _reader, session_factory = api_client
    seeded = _seed_contacts(session_factory, tenant_a["tenant_id"], count=3)
    session = session_factory()
    contact = session.get(Contact, seeded[0][0])
    contact.verification_status = "INVALID"
    contact.risk_level = "HIGH"
    session.commit()
    session.close()

    token = _login(client, tenant_a["email"])
    response = client.get(
        "/api/v1/contacts?verification_status=INVALID", headers=_headers(token)
    )
    assert response.status_code == 200
    body = response.json()
    assert body["total"] == 1
    assert body["items"][0]["email"] == seeded[0][1]

    by_risk = client.get("/api/v1/contacts?risk_level=HIGH", headers=_headers(token))
    assert by_risk.json()["total"] == 1


def test_bulk_verify_requires_write_permission(api_client) -> None:
    client, _a, _b, reader_a, session_factory = api_client
    seeded = _seed_contacts(session_factory, reader_a["tenant_id"], count=1)
    token = _login(client, reader_a["email"])
    response = client.post(
        "/api/v1/contacts/bulk-verify",
        headers=_headers(token),
        json={"contact_ids": [str(cid) for cid, _ in seeded]},
    )
    assert response.status_code in (401, 403)
