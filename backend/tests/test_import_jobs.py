from __future__ import annotations

import io
from datetime import UTC, datetime, timedelta
from typing import cast
from uuid import UUID, uuid4

import pytest
from fastapi.testclient import TestClient
from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session, sessionmaker

from app.api import imports as imports_api
from app.core.database import get_db
from app.main import IMPORT_JOB_STATUS_BUCKET, app, rate_limit_for
from app.models import (
    Base,
    Contact,
    ContactFieldDefinition,
    ImportJob,
    Permission,
    Role,
    RolePermission,
    Suppression,
    Tenant,
    User,
    UserRole,
    VerificationJob,
)
from app.security.passwords import hash_password
from app.workers import imports as worker_imports

CSV_HEADER = "first_name,email,company,skills\n"


@pytest.fixture()
def import_api(tmp_path, monkeypatch):
    engine = create_engine(f"sqlite:///{tmp_path / 'imports_api.db'}", connect_args={"check_same_thread": False})
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)
    session = session_factory()

    read_permission = Permission(key="contacts.read", description="Read contacts")
    write_permission = Permission(key="contacts.create", description="Create contacts")
    session.add_all([read_permission, write_permission])
    session.flush()

    def make_tenant(name: str, email: str, role_name: str = "Admin", grant_read: bool = False, grant_write: bool = False, tenant_id=None) -> dict:
        if tenant_id is not None:
            tenant = session.get(Tenant, tenant_id)
        else:
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
        if grant_write:
            session.add(RolePermission(role_id=role.id, permission_id=write_permission.id))
        session.flush()
        return {"tenant_id": tenant.id, "user_id": user.id, "email": email}

    tenant_a = make_tenant("Acme", "admin.a@example.com")
    tenant_b = make_tenant("Globex", "admin.b@example.com")
    viewer_a = make_tenant("Acme Viewer", "viewer.a@example.com", role_name="Viewer", grant_read=True, grant_write=False, tenant_id=tenant_a["tenant_id"])
    session.commit()
    session.close()

    def override_db():
        db = session_factory()
        try:
            yield db
        finally:
            db.close()

    app.dependency_overrides[get_db] = override_db
    monkeypatch.setattr(imports_api, "UPLOAD_DIR", tmp_path / "uploads" / "imports")

    def run_validate(job_id, tenant_id, source_file):
        worker_imports.process_validate(job_id, tenant_id, source_file, session_factory=session_factory)

    def run_import(job_id, tenant_id, source_file, mapping=None, policy="SKIP"):
        worker_imports.process_import(job_id, tenant_id, source_file, mapping, policy, session_factory=session_factory)

    monkeypatch.setattr(imports_api, "enqueue_validate", run_validate)
    monkeypatch.setattr(imports_api, "enqueue_import", run_import)

    client = TestClient(app)
    yield client, session_factory, tenant_a, tenant_b, viewer_a
    app.dependency_overrides.clear()
    engine.dispose()


def _login(client: TestClient, email: str) -> str:
    response = client.post("/api/v1/auth/login", json={"email": email, "password": "correct horse battery staple"})
    assert response.status_code == 200, response.text
    return response.json()["access_token"]


def _headers(token: str) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


def _csv_bytes(content: str) -> bytes:
    return (content if content.endswith("\n") else content + "\n").encode()


def _upload(client: TestClient, token: str, filename: str, content: bytes, media: str = "text/csv") -> dict:
    response = client.post("/api/v1/contacts/imports", headers=_headers(token), files={"file": (filename, content, media)})
    assert response.status_code == 202, response.text
    return response.json()


def _wait_ready(client: TestClient, token: str, job_id: str) -> dict:
    for _ in range(20):
        job = client.get(f"/api/v1/contacts/imports/{job_id}", headers=_headers(token)).json()
        if job["status"] not in {"UPLOADED", "VALIDATING"}:
            return job
    raise AssertionError("Job never left VALIDATING")


def _start(client: TestClient, token: str, job_id: str) -> dict:
    response = client.post(f"/api/v1/contacts/imports/{job_id}/start", headers=_headers(token))
    assert response.status_code == 200, response.text
    return response.json()


def _xlsx_bytes(headers: list[str], rows: list[list[object]]) -> bytes:
    workbook = Workbook()
    sheet = cast(Worksheet, workbook.active)
    sheet.append(headers)
    for row in rows:
        sheet.append(row)
    output = io.BytesIO()
    workbook.save(output)
    return output.getvalue()


def test_csv_import_full_cycle(import_api) -> None:
    client, session_factory, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    job = _upload(client, token, "recipients.csv", _csv_bytes(CSV_HEADER + "Ada,ada@example.com,Engine Co,Python\nGrace,grace@example.com,Engine Co,Golang\n"))
    job = _wait_ready(client, token, job["id"])
    assert job["status"] == "READY"
    assert job["total_rows"] == 2
    assert job["column_mapping"]["email"] == "email"
    assert job["column_mapping"]["first_name"] == "first_name"

    update = client.put(
        f"/api/v1/contacts/imports/{job['id']}",
        headers=_headers(token),
        json={"column_mapping": {"email": "email", "first_name": "first_name", "company": "company", "skills": "skills"}, "duplicate_policy": "SKIP"},
    )
    assert update.status_code == 200, update.text

    _start(client, token, job["id"])
    finished = client.get(f"/api/v1/contacts/imports/{job['id']}", headers=_headers(token)).json()
    assert finished["status"] == "COMPLETED"
    assert finished["successful_rows"] == 2
    assert finished["processed_rows"] == 2

    with session_factory() as session:
        contacts = session.query(Contact).filter(Contact.tenant_id == tenant_a["tenant_id"]).order_by(Contact.email).all()
        assert [contact.email for contact in contacts] == ["ada@example.com", "grace@example.com"]
        assert all(contact.source == "import" for contact in contacts)
        skills = {field.field_key: field.field_value for field in contacts[0].custom_fields}
        assert skills == {"skills": "Python"}


def test_xlsx_import_full_cycle(import_api) -> None:
    client, session_factory, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    content = _xlsx_bytes(["Email", "First Name"], [["grace@example.com", "Grace"]])
    job = _upload(client, token, "recipients.xlsx", content, media="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    job = _wait_ready(client, token, job["id"])
    assert job["status"] == "READY"
    assert job["file_type"] == "xlsx"
    _start(client, token, job["id"])
    finished = client.get(f"/api/v1/contacts/imports/{job['id']}", headers=_headers(token)).json()
    assert finished["status"] == "COMPLETED"
    with session_factory() as session:
        assert session.query(Contact).filter(Contact.tenant_id == tenant_a["tenant_id"]).count() == 1


def test_job_status_polling_uses_its_own_budget() -> None:
    job_id = "b104d910-8dcb-4354-8c76-0284871531c0"
    poll_bucket, poll_limit, _ = rate_limit_for("GET", f"/api/v1/contacts/imports/{job_id}")
    upload_bucket, upload_limit, _ = rate_limit_for("POST", "/api/v1/contacts/imports")
    assert poll_bucket == IMPORT_JOB_STATUS_BUCKET
    assert poll_bucket != upload_bucket
    assert poll_limit > upload_limit


def test_import_writes_keep_the_upload_budget() -> None:
    job_id = "b104d910-8dcb-4354-8c76-0284871531c0"
    for method, route in (
        ("POST", "/api/v1/contacts/imports"),
        ("PUT", f"/api/v1/contacts/imports/{job_id}"),
        ("POST", f"/api/v1/contacts/imports/{job_id}/start"),
        ("POST", f"/api/v1/contacts/imports/{job_id}/retry"),
        ("POST", f"/api/v1/contacts/imports/{job_id}/cancel"),
        ("GET", f"/api/v1/contacts/imports/{job_id}/errors"),
    ):
        assert rate_limit_for(method, route) == ("/api/v1/contacts/imports", 10, 60), route


def test_upload_rejects_invalid_extension(import_api) -> None:
    client, _, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    response = client.post("/api/v1/contacts/imports", headers=_headers(token), files={"file": ("recipients.pdf", b"%PDF-1.4 data", "application/pdf")})
    assert response.status_code == 400
    assert "CSV and XLSX" in response.json()["detail"]


def test_upload_rejects_binary_disguised_as_csv(import_api) -> None:
    client, _, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    response = client.post("/api/v1/contacts/imports", headers=_headers(token), files={"file": ("recipients.csv", b"\x00\x00\x01\x00windows", "text/csv")})
    assert response.status_code == 400
    assert "text" in response.json()["detail"]


def test_upload_rejects_fake_xlsx(import_api) -> None:
    client, _, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    response = client.post("/api/v1/contacts/imports", headers=_headers(token), files={"file": ("recipients.xlsx", b"not really a workbook", "application/octet-stream")})
    assert response.status_code == 400
    assert "XLSX" in response.json()["detail"]


def test_upload_rejects_path_traversal_filename(import_api) -> None:
    client, _, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    response = client.post("/api/v1/contacts/imports", headers=_headers(token), files={"file": ("../evil.csv", _csv_bytes(CSV_HEADER + "Evil,evil@example.com,\n"), "text/csv")})
    assert response.status_code == 400


def test_upload_rejects_oversized_file(import_api) -> None:
    client, _, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    response = client.post("/api/v1/contacts/imports", headers=_headers(token), files={"file": ("big.csv", b"a" * (10 * 1024 * 1024 + 1), "text/csv")})
    assert response.status_code == 400
    assert "10 MB" in response.json()["detail"]


def test_viewer_cannot_upload_but_can_read(import_api) -> None:
    client, _, tenant_a, _, viewer_a = import_api
    token = _login(client, tenant_a["email"])
    viewer_token = _login(client, viewer_a["email"])
    job = _upload(client, token, "recipients.csv", _csv_bytes(CSV_HEADER + "Ada,ada@example.com,\n"))
    blocked = client.post("/api/v1/contacts/imports", headers=_headers(viewer_token), files={"file": ("x.csv", _csv_bytes(CSV_HEADER + "v,v@example.com,\n"), "text/csv")})
    assert blocked.status_code == 403
    assert client.get(f"/api/v1/contacts/imports/{job['id']}", headers=_headers(viewer_token)).status_code == 200
    assert client.post(f"/api/v1/contacts/imports/{job['id']}/start", headers=_headers(viewer_token)).status_code == 403


def test_validation_fails_without_email_column(import_api) -> None:
    client, _, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    job = _upload(client, token, "noemail.csv", _csv_bytes("first_name,company\nAda,Eng Co\n"))
    job = _wait_ready(client, token, job["id"])
    assert job["status"] == "FAILED"
    assert "email" in job["error_message"].lower()


def test_validation_fails_when_too_many_rows(import_api) -> None:
    client, _, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    rows = "\n".join(f"Person{index},person{index}@example.com,Co," for index in range(100_001))
    job = _upload(client, token, "huge.csv", _csv_bytes(f"{CSV_HEADER}{rows}"))
    job = _wait_ready(client, token, job["id"])
    assert job["status"] == "FAILED"
    assert "100,000" in job["error_message"]


def test_invalid_rows_produce_error_report(import_api) -> None:
    client, _, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    job = _upload(client, token, "bad.csv", _csv_bytes(CSV_HEADER + "Bad,not-an-email,Co,\nGood,good@example.com,Co,\n"))
    job = _wait_ready(client, token, job["id"])
    _start(client, token, job["id"])
    finished = client.get(f"/api/v1/contacts/imports/{job['id']}", headers=_headers(token)).json()
    assert finished["status"] == "COMPLETED_WITH_ERRORS"
    assert finished["successful_rows"] == 1
    assert finished["failed_rows"] == 1

    errors = client.get(f"/api/v1/contacts/imports/{job['id']}/errors", headers=_headers(token))
    assert errors.status_code == 200
    assert "text/csv" in errors.headers["content-type"]
    assert "invalid" in errors.text
    assert "2" in errors.text


def test_duplicates_and_existing_contacts_skipped(import_api) -> None:
    client, session_factory, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    with session_factory() as session:
        session.add(Contact(tenant_id=tenant_a["tenant_id"], email="existing@example.com", company="Old"))
        session.commit()
    job = _upload(client, token, "dup.csv", _csv_bytes(CSV_HEADER + "Ada,ada@example.com,Co,\nAda,ada@example.com,Co,\nExisting,existing@example.com,Co,\n"))
    job = _wait_ready(client, token, job["id"])
    _start(client, token, job["id"])
    finished = client.get(f"/api/v1/contacts/imports/{job['id']}", headers=_headers(token)).json()
    assert finished["status"] == "COMPLETED"
    assert finished["successful_rows"] == 1
    assert finished["duplicate_rows"] == 2
    with session_factory() as session:
        contacts = session.query(Contact).filter(Contact.tenant_id == tenant_a["tenant_id"]).all()
        assert len(contacts) == 2
        existing = next(contact for contact in contacts if contact.email == "existing@example.com")
        assert existing.company == "Old"


def test_update_policy_updates_existing_contact(import_api) -> None:
    client, session_factory, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    with session_factory() as session:
        session.add(Contact(tenant_id=tenant_a["tenant_id"], email="ada@example.com", company="Old"))
        session.commit()
    job = _upload(client, token, "upd.csv", _csv_bytes(CSV_HEADER + "Ada,ada@example.com,New Co,\n"))
    job = _wait_ready(client, token, job["id"])
    update = client.put(f"/api/v1/contacts/imports/{job['id']}", headers=_headers(token), json={"duplicate_policy": "UPDATE"})
    assert update.status_code == 200
    _start(client, token, job["id"])
    finished = client.get(f"/api/v1/contacts/imports/{job['id']}", headers=_headers(token)).json()
    assert finished["status"] == "COMPLETED"
    assert finished["updated_rows"] == 1
    assert finished["successful_rows"] == 1
    with session_factory() as session:
        contact = session.query(Contact).filter(Contact.tenant_id == tenant_a["tenant_id"]).one()
        assert contact.company == "New Co"
        assert contact.first_name == "Ada"


def test_create_new_policy_keeps_single_contact(import_api) -> None:
    client, session_factory, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    with session_factory() as session:
        session.add(Contact(tenant_id=tenant_a["tenant_id"], email="ada@example.com", company="Old"))
        session.commit()
    job = _upload(client, token, "cn.csv", _csv_bytes(CSV_HEADER + "Ada,ada@example.com,Other,\n"))
    job = _wait_ready(client, token, job["id"])
    client.put(f"/api/v1/contacts/imports/{job['id']}", headers=_headers(token), json={"duplicate_policy": "CREATE_NEW"})
    _start(client, token, job["id"])
    finished = client.get(f"/api/v1/contacts/imports/{job['id']}", headers=_headers(token)).json()
    assert finished["status"] == "COMPLETED"
    assert finished["duplicate_rows"] == 1
    with session_factory() as session:
        assert session.query(Contact).filter(Contact.tenant_id == tenant_a["tenant_id"]).count() == 1


def test_suppressed_contacts_are_skipped(import_api) -> None:
    client, session_factory, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    with session_factory() as session:
        session.add(Suppression(tenant_id=tenant_a["tenant_id"], email="blocked@example.com", reason="MANUAL_BLOCK", source="test", effective_at=datetime.now(UTC)))
        session.commit()
    job = _upload(client, token, "sup.csv", _csv_bytes(CSV_HEADER + "Blocked,blocked@example.com,Co,\nOk,ok@example.com,Co,\n"))
    job = _wait_ready(client, token, job["id"])
    _start(client, token, job["id"])
    finished = client.get(f"/api/v1/contacts/imports/{job['id']}", headers=_headers(token)).json()
    assert finished["status"] == "COMPLETED"
    assert finished["suppressed_rows"] == 1
    assert finished["successful_rows"] == 1
    with session_factory() as session:
        assert session.query(Contact).filter(Contact.tenant_id == tenant_a["tenant_id"], Contact.email == "blocked@example.com").count() == 0
        assert session.query(Suppression).filter(Suppression.tenant_id == tenant_a["tenant_id"], Suppression.email == "blocked@example.com").count() == 1


def test_field_typed_custom_values_are_validated(import_api) -> None:
    client, session_factory, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    with session_factory() as session:
        session.add(ContactFieldDefinition(tenant_id=tenant_a["tenant_id"], key="budget", label="Budget", field_type="NUMBER", options=[], required=False))
        session.commit()
    headers = "first_name,email,budget"
    job = _upload(client, token, "cf.csv", _csv_bytes(f"{headers}\nAda,ada@example.com,lots\nGrace,grace@example.com,42\n"))
    job = _wait_ready(client, token, job["id"])
    _start(client, token, job["id"])
    finished = client.get(f"/api/v1/contacts/imports/{job['id']}", headers=_headers(token)).json()
    assert finished["status"] == "COMPLETED_WITH_ERRORS"
    assert finished["successful_rows"] == 1
    assert finished["failed_rows"] == 1
    with session_factory() as session:
        contact = session.query(Contact).filter(Contact.tenant_id == tenant_a["tenant_id"], Contact.email == "grace@example.com").one()
        assert {field.field_key: field.field_value for field in contact.custom_fields} == {"budget": "42"}


def test_cross_tenant_isolation(import_api) -> None:
    client, session_factory, tenant_a, tenant_b, _ = import_api
    token_a = _login(client, tenant_a["email"])
    token_b = _login(client, tenant_b["email"])
    job = _upload(client, token_a, "a.csv", _csv_bytes(CSV_HEADER + "Ada,ada@example.com,Co,\n"))
    job_id = job["id"]
    headers_b = _headers(token_b)
    assert client.get(f"/api/v1/contacts/imports/{job_id}", headers=headers_b).status_code == 404
    assert client.put(f"/api/v1/contacts/imports/{job_id}", headers=headers_b, json={"duplicate_policy": "UPDATE"}).status_code == 404
    assert client.post(f"/api/v1/contacts/imports/{job_id}/start", headers=headers_b).status_code == 404
    assert client.post(f"/api/v1/contacts/imports/{job_id}/cancel", headers=headers_b).status_code == 404
    assert client.get(f"/api/v1/contacts/imports/{job_id}/errors", headers=headers_b).status_code == 404
    with session_factory() as session:
        assert session.query(Contact).filter(Contact.tenant_id == tenant_b["tenant_id"]).count() == 0


def test_mapping_update_validation_rules(import_api) -> None:
    client, session_factory, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    with session_factory() as session:
        session.add(ContactFieldDefinition(tenant_id=tenant_a["tenant_id"], key="budget", label="Budget", field_type="TEXT", options=[], required=False))
        session.commit()
    job = _upload(client, token, "m.csv", _csv_bytes(CSV_HEADER + "Ada,ada@example.com,Co,\n"))
    job = _wait_ready(client, token, job["id"])
    job_id = job["id"]

    unknown = client.put(f"/api/v1/contacts/imports/{job_id}", headers=_headers(token), json={"column_mapping": {"email": "email", "bogus": "company"}})
    assert unknown.status_code == 400
    no_email = client.put(f"/api/v1/contacts/imports/{job_id}", headers=_headers(token), json={"column_mapping": {"first_name": "email"}})
    assert no_email.status_code == 400
    bad_policy = client.put(f"/api/v1/contacts/imports/{job_id}", headers=_headers(token), json={"duplicate_policy": "WHATEVER"})
    assert bad_policy.status_code == 400

    ok = client.put(
        f"/api/v1/contacts/imports/{job_id}",
        headers=_headers(token),
        json={"column_mapping": {"email": "email", "company": "company", "budget": "company"}, "duplicate_policy": "UPDATE"},
    )
    assert ok.status_code == 200
    assert ok.json()["duplicate_policy"] == "UPDATE"

    _start(client, token, job_id)
    after_start = client.put(f"/api/v1/contacts/imports/{job_id}", headers=_headers(token), json={"duplicate_policy": "SKIP"})
    assert after_start.status_code == 409


def test_cancel_then_retry_import(import_api) -> None:
    client, session_factory, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    job = _upload(client, token, "r.csv", _csv_bytes(CSV_HEADER + "Ada,ada@example.com,Co,\n"))
    job = _wait_ready(client, token, job["id"])
    job_id = job["id"]

    cancelled = client.post(f"/api/v1/contacts/imports/{job_id}/cancel", headers=_headers(token))
    assert cancelled.status_code == 200
    assert cancelled.json()["status"] == "CANCELLED"
    assert client.post(f"/api/v1/contacts/imports/{job_id}/start", headers=_headers(token)).status_code == 409
    assert client.post(f"/api/v1/contacts/imports/{job_id}/cancel", headers=_headers(token)).status_code == 409

    retried = client.post(f"/api/v1/contacts/imports/{job_id}/retry", headers=_headers(token))
    assert retried.status_code == 200
    assert retried.json()["status"] == "READY"

    _start(client, token, job_id)
    finished = client.get(f"/api/v1/contacts/imports/{job_id}", headers=_headers(token)).json()
    assert finished["status"] == "COMPLETED"
    with session_factory() as session:
        assert session.query(Contact).filter(Contact.tenant_id == tenant_a["tenant_id"]).count() == 1


def test_worker_failure_marks_job_failed_then_recovers(import_api) -> None:
    client, _, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    job = _upload(client, token, "f.csv", _csv_bytes(CSV_HEADER + "Ada,ada@example.com,Co,\n"))
    job = _wait_ready(client, token, job["id"])
    job_id = job["id"]

    stored = next(path for path in imports_api.UPLOAD_DIR.glob("**/*.csv"))
    original = stored.read_bytes()
    stored.write_bytes(b"broken,no-email-column\n")

    _start(client, token, job_id)
    finished = client.get(f"/api/v1/contacts/imports/{job_id}", headers=_headers(token)).json()
    assert finished["status"] == "FAILED"
    assert finished["error_message"]

    stored.write_bytes(original)
    retried = client.post(f"/api/v1/contacts/imports/{job_id}/retry", headers=_headers(token))
    assert retried.status_code == 200
    assert retried.json()["status"] == "READY"
    _start(client, token, job_id)
    finished = client.get(f"/api/v1/contacts/imports/{job_id}", headers=_headers(token)).json()
    assert finished["status"] == "COMPLETED"


def test_missing_source_file_blocks_start(import_api) -> None:
    client, _, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])
    job = _upload(client, token, "m.csv", _csv_bytes(CSV_HEADER + "Ada,ada@example.com,Co,\n"))
    job = _wait_ready(client, token, job["id"])

    for path in imports_api.UPLOAD_DIR.glob("**/*"):
        if path.is_file():
            path.unlink()
    response = client.post(f"/api/v1/contacts/imports/{job['id']}/start", headers=_headers(token))
    assert response.status_code == 409


def test_enqueue_dispatches_to_the_celery_broker(monkeypatch) -> None:
    """Imports ride the existing queue so a deploy cannot drop a queued job."""
    sent: list[tuple[str, list[str]]] = []

    class _Result:
        id = "task-abc"

    def fake_send_task(name: str, args: list[str] | None = None, **_kwargs: object) -> _Result:
        sent.append((name, list(args or [])))
        return _Result()

    monkeypatch.setattr(
        "app.tasks.scheduler.celery_app.send_task", fake_send_task, raising=False
    )

    job_id, tenant_id = uuid4(), uuid4()
    worker_imports.enqueue_validate(job_id, tenant_id, "/tmp/file.csv")
    worker_imports.enqueue_import(job_id, tenant_id, "/tmp/file.csv", {"email": "Email"}, "SKIP")

    assert [name for name, _ in sent] == [
        worker_imports.TASK_VALIDATE,
        worker_imports.TASK_IMPORT,
    ]
    assert all(str(job_id) in args for _name, args in sent)
    assert all(str(tenant_id) in args for _name, args in sent)


def test_a_finished_import_queues_verification_for_the_rows_it_created(
    monkeypatch, tmp_path
) -> None:
    """Importing must kick off validation for the new contacts, scoped to them."""
    engine = create_engine(f"sqlite:///{tmp_path / 'post-import.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    tenant_id = uuid4()
    actor_id = uuid4()
    with session_factory() as session:
        session.add(Tenant(id=tenant_id, name="Acme", slug="acme"))
        session.add(
            User(
                id=actor_id,
                tenant_id=tenant_id,
                email="owner@acme.com",
                password_hash=hash_password("Sup3rSecret!"),
                display_name="Owner",
                status="ACTIVE",
            )
        )
        session.commit()

    source = tmp_path / "rows.csv"
    source.write_text("email,first_name\nnew@acme.com,New\n", encoding="utf-8")

    sent: list[tuple[str, list[str]]] = []

    class _Result:
        id = "verify-task"

    def fake_send_task(name: str, args: list[str] | None = None, **_kwargs: object) -> _Result:
        sent.append((name, list(args or [])))
        return _Result()

    monkeypatch.setattr(
        "app.tasks.scheduler.celery_app.send_task", fake_send_task, raising=False
    )

    job_id = uuid4()
    with session_factory() as session:
        session.add(
            ImportJob(
                id=job_id,
                tenant_id=tenant_id,
                created_by_id=actor_id,
                filename="rows.csv",
                file_type="csv",
                status="READY",
            )
        )
        session.commit()

    worker_imports.process_import(
        job_id, tenant_id, str(source), {"email": "email"}, "SKIP", session_factory
    )

    with session_factory() as session:
        job = session.get(ImportJob, job_id)
        assert job.status == "COMPLETED"
        assert job.successful_rows == 1
        contacts = list(session.scalars(Contact.__table__.select()).all())
        assert contacts

    with session_factory() as session:
        verification_jobs = list(session.query(VerificationJob).all())
        assert len(verification_jobs) == 1
        job_row = verification_jobs[0]
    assert job_row.tenant_id == tenant_id
    assert job_row.celery_task_id == "verify-task"
    filters = (job_row.request_payload or {}).get("filters") or {}
    assert "created_after" in filters
    assert job_row.total_count == 1

    assert [name for name, _ in sent] == [worker_imports.TASK_VERIFY]
    assert str(tenant_id) in sent[0][1]
    assert str(job_row.id) in sent[0][1]


def test_a_finished_import_stays_completed_when_verification_handoff_fails(
    monkeypatch, tmp_path
) -> None:
    """Committed rows must not be reported as a failed import."""
    engine = create_engine(f"sqlite:///{tmp_path / 'handoff-fail.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    tenant_id = uuid4()
    actor_id = uuid4()
    with session_factory() as session:
        session.add(Tenant(id=tenant_id, name="Acme", slug="acme"))
        session.add(
            User(
                id=actor_id,
                tenant_id=tenant_id,
                email="owner@acme.com",
                password_hash=hash_password("Sup3rSecret!"),
                display_name="Owner",
                status="ACTIVE",
            )
        )
        session.commit()

    source = tmp_path / "rows.csv"
    source.write_text("email,first_name\nnew@acme.com,New\n", encoding="utf-8")

    def boom(*_args: object, **_kwargs: object) -> None:
        raise RuntimeError("broker unreachable")

    monkeypatch.setattr(worker_imports, "enqueue_verification", boom)

    job_id = uuid4()
    with session_factory() as session:
        session.add(
            ImportJob(
                id=job_id,
                tenant_id=tenant_id,
                created_by_id=actor_id,
                filename="rows.csv",
                file_type="csv",
                status="READY",
            )
        )
        session.commit()

    worker_imports.process_import(
        job_id, tenant_id, str(source), {"email": "email"}, "SKIP", session_factory
    )

    with session_factory() as session:
        job = session.get(ImportJob, job_id)
        assert job.status == "COMPLETED"
        assert job.successful_rows == 1
        assert job.finished_at is not None
        assert session.query(Contact).count() == 1


def test_a_duplicate_only_import_does_not_queue_verification(
    monkeypatch, tmp_path
) -> None:
    """A run that changed nothing has nothing new to verify."""
    engine = create_engine(f"sqlite:///{tmp_path / 'post-import-empty.db'}")
    Base.metadata.create_all(engine)
    session_factory = sessionmaker(bind=engine, expire_on_commit=False)

    tenant_id = uuid4()
    actor_id = uuid4()
    with session_factory() as session:
        session.add(Tenant(id=tenant_id, name="Acme", slug="acme"))
        session.add(
            User(
                id=actor_id,
                tenant_id=tenant_id,
                email="owner@acme.com",
                password_hash=hash_password("Sup3rSecret!"),
                display_name="Owner",
                status="ACTIVE",
            )
        )
        session.commit()

    source = tmp_path / "bad.csv"
    source.write_text("email\nnot-an-email\n", encoding="utf-8")

    sent: list[str] = []

    class _Result:
        id = "verify-task"

    def fake_send_task(name: str, args: list[str] | None = None, **_kwargs: object) -> _Result:
        sent.append(name)
        return _Result()

    monkeypatch.setattr(
        "app.tasks.scheduler.celery_app.send_task", fake_send_task, raising=False
    )

    job_id = uuid4()
    with session_factory() as session:
        session.add(
            ImportJob(
                id=job_id,
                tenant_id=tenant_id,
                created_by_id=actor_id,
                filename="bad.csv",
                file_type="csv",
                status="READY",
            )
        )
        session.commit()

    worker_imports.process_import(
        job_id, tenant_id, str(source), {"email": "email"}, "SKIP", session_factory
    )

    with session_factory() as session:
        assert session.get(ImportJob, job_id).successful_rows == 0
        assert session.query(VerificationJob).all() == []
    assert sent == []


def test_stale_imports_are_reclaimed_by_heartbeat(tmp_path) -> None:
    """A job whose worker stopped heartbeating is re-queued, started or not."""
    engine = create_engine(f"sqlite:///{tmp_path / 'reclaim.db'}")
    Base.metadata.create_all(engine)
    source = tmp_path / "live.csv"
    source.write_text(CSV_HEADER + "Ada,ada@example.com,Co,\n", encoding="utf-8")
    dispatched: list[tuple[str, object]] = []

    with Session(engine) as session:
        tenant = Tenant(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        stale_importing = ImportJob(
            tenant_id=tenant.id,
            filename="live.csv",
            file_type="csv",
            status="IMPORTING",
            started_at=datetime.now(UTC) - timedelta(minutes=30),
            updated_at=datetime.now(UTC) - timedelta(minutes=30),
            source_file_ref=str(source),
        )
        fresh_importing = ImportJob(
            tenant_id=tenant.id,
            filename="live.csv",
            file_type="csv",
            status="IMPORTING",
            started_at=datetime.now(UTC),
            updated_at=datetime.now(UTC),
            source_file_ref=str(source),
        )
        stale_validating = ImportJob(
            tenant_id=tenant.id,
            filename="live.csv",
            file_type="csv",
            status="VALIDATING",
            updated_at=datetime.now(UTC) - timedelta(minutes=30),
            source_file_ref=str(source),
        )
        session.add_all([stale_importing, fresh_importing, stale_validating])
        session.commit()

        import app.tasks.scheduler as scheduler_module

        monkey_dispatch = scheduler_module.celery_app.send_task
        scheduler_module.celery_app.send_task = lambda name, args=None, **kw: dispatched.append(
            (name, args)
        ) or type("R", (), {"id": "x"})()
        try:
            result = worker_imports.reclaim_stale_imports(session)
        finally:
            scheduler_module.celery_app.send_task = monkey_dispatch

        assert result["requeued"] == 2
        assert [name for name, _ in dispatched] == [
            worker_imports.TASK_IMPORT,
            worker_imports.TASK_VALIDATE,
        ]
        # The healthy job is left alone.
        assert fresh_importing.status == "IMPORTING"
        # The re-queued job keeps its original started_at so elapsed time is honest.
        assert stale_importing.started_at is not None
    engine.dispose()


def test_reclaim_fails_a_job_whose_file_is_gone(tmp_path) -> None:
    engine = create_engine(f"sqlite:///{tmp_path / 'gone.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        job = ImportJob(
            tenant_id=tenant.id,
            filename="gone.csv",
            file_type="csv",
            status="IMPORTING",
            started_at=datetime.now(UTC) - timedelta(minutes=30),
            updated_at=datetime.now(UTC) - timedelta(minutes=30),
            source_file_ref=str(tmp_path / "missing.csv"),
        )
        session.add(job)
        session.commit()

        result = worker_imports.reclaim_stale_imports(session)
        assert result["requeued"] == 0
        session.refresh(job)
        assert job.status == "FAILED"
        assert job.finished_at is not None
    engine.dispose()


def test_stuck_uploaded_job_is_re_enqueued_on_poll(import_api, monkeypatch) -> None:
    client, session_factory, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])

    calls = []
    monkeypatch.setattr(imports_api, "enqueue_validate", lambda job_id, tenant_id, source_file: calls.append(job_id))
    monkeypatch.setattr(imports_api, "enqueue_import", lambda *_args, **_kwargs: None)

    job = _upload(client, token, "s.csv", _csv_bytes(CSV_HEADER + "Ada,ada@example.com,Co,\n"))
    calls.clear()

    with session_factory() as session:
        row = session.get(ImportJob, UUID(job["id"]))
        assert row is not None
        # The heartbeat is updated_at, which every progress batch refreshes.
        row.updated_at = datetime.now(UTC) - timedelta(minutes=5)
        session.commit()

    response = client.get(f"/api/v1/contacts/imports/{job['id']}", headers=_headers(token))
    assert response.status_code == 200
    assert [str(call) for call in calls] == [job["id"]]


def test_fresh_heartbeat_job_is_not_re_enqueued_on_poll(import_api, monkeypatch) -> None:
    """A live import must never be double-queued by a status poll."""
    client, session_factory, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])

    enqueued = []
    monkeypatch.setattr(imports_api, "enqueue_validate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(imports_api, "enqueue_import", lambda job_id, *_args, **_kwargs: enqueued.append(job_id))

    job = _upload(client, token, "r.csv", _csv_bytes(CSV_HEADER + "Ada,ada@example.com,Co,\n"))
    job_id = job["id"]
    with session_factory() as session:
        row = session.get(ImportJob, UUID(job_id))
        row.status = "IMPORTING"
        # An old start time is irrelevant: the heartbeat, not the start, decides.
        row.started_at = datetime.now(UTC) - timedelta(minutes=30)
        row.updated_at = datetime.now(UTC)
        session.commit()

    client.get(f"/api/v1/contacts/imports/{job_id}", headers=_headers(token))
    assert enqueued == []


def test_stale_importing_job_is_re_enqueued_even_after_it_started(import_api, monkeypatch) -> None:
    """The old ``started_at is None`` guard deadlocked jobs killed mid-import."""
    client, session_factory, tenant_a, _, _ = import_api
    token = _login(client, tenant_a["email"])

    enqueued = []
    monkeypatch.setattr(imports_api, "enqueue_validate", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(imports_api, "enqueue_import", lambda job_id, *_args, **_kwargs: enqueued.append(job_id))

    job = _upload(client, token, "t.csv", _csv_bytes(CSV_HEADER + "Ada,ada@example.com,Co,\n"))
    job_id = job["id"]
    with session_factory() as session:
        row = session.get(ImportJob, UUID(job_id))
        row.status = "IMPORTING"
        row.started_at = datetime.now(UTC) - timedelta(minutes=5)
        row.updated_at = datetime.now(UTC) - timedelta(minutes=5)
        session.commit()

    client.get(f"/api/v1/contacts/imports/{job_id}", headers=_headers(token))
    assert [str(call) for call in enqueued] == [job_id]