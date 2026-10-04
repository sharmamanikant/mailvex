from __future__ import annotations

import io
from typing import cast
from uuid import uuid4

import dns.exception
import pytest
from openpyxl import Workbook
from openpyxl.worksheet.worksheet import Worksheet
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Contact, Suppression, Tenant
from app.services import imports as imports_module
from app.services.contacts import ContactService
from app.services.imports import (
    MAX_UNIQUE_DNS_LOOKUPS,
    ImportService,
    ImportValidationError,
)


@pytest.fixture()
def import_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'imports.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Import Tenant", slug=f"imports-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        yield session, tenant.id
    engine.dispose()


def csv_content(rows: list[str]) -> bytes:
    return ("first_name,email,company,skills\n" + "\n".join(rows) + "\n").encode()


def test_csv_import_and_mapping(import_session) -> None:
    session, tenant_id = import_session
    service = ImportService(session, tenant_id)
    headers, rows = service.headers_and_rows("recipients.csv", csv_content(["Ada,ada@example.com,Engine Co,Python"]))
    assert service.detect_mapping(headers)["email"] == "email"
    report = service.import_rows(headers, rows)
    assert report.imported == 1
    contact = session.query(Contact).one()
    assert contact.email == "ada@example.com"
    assert contact.custom_fields[0].field_key == "skills"


def test_xlsx_import(import_session) -> None:
    session, tenant_id = import_session
    workbook = Workbook()
    sheet = cast(Worksheet, workbook.active)
    sheet.append(["Email", "First Name", "Technology"])
    sheet.append(["grace@example.com", "Grace", "Python"])
    output = io.BytesIO()
    workbook.save(output)
    service = ImportService(session, tenant_id)
    headers, rows = service.headers_and_rows("recipients.xlsx", output.getvalue())
    report = service.import_rows(headers, rows)
    assert report.imported == 1
    assert session.query(Contact).one().email == "grace@example.com"


def test_import_splits_full_name_column(import_session) -> None:
    session, tenant_id = import_session
    service = ImportService(session, tenant_id)
    headers, rows = service.headers_and_rows("recipients.csv", b"Name,Email,Company\nAda Lovelace,ada-lovelace@example.com,Engine Co\n")
    report = service.import_rows(headers, rows)
    assert report.imported == 1
    contact = session.query(Contact).one()
    assert contact.first_name == "Ada"
    assert contact.last_name == "Lovelace"


def test_invalid_file_is_rejected(import_session) -> None:
    session, tenant_id = import_session
    with pytest.raises(ImportValidationError, match="CSV and XLSX"):
        ImportService(session, tenant_id).headers_and_rows("recipients.pdf", b"data")


def test_duplicate_invalid_and_suppressed_rows(import_session) -> None:
    session, tenant_id = import_session
    session.add(Contact(tenant_id=tenant_id, email="existing@example.com"))
    session.add(Suppression(tenant_id=tenant_id, email="blocked@example.com", reason="MANUAL_BLOCK", source="test", effective_at=__import__("datetime").datetime.now(__import__("datetime").timezone.utc)))
    session.commit()
    service = ImportService(session, tenant_id)
    headers, rows = service.headers_and_rows("recipients.csv", csv_content(["Existing,existing@example.com,Co,", "Blocked,blocked@example.com,Co,", "Invalid,not-an-email,Co,", "Duplicate,new@example.com,Co,", "Duplicate,new@example.com,Co,"]))
    report = service.import_rows(headers, rows)
    assert report.duplicate == 2
    assert report.suppressed == 1
    assert report.invalid == 1
    assert report.imported == 1


def test_bulk_delete_removes_selected_contacts(import_session) -> None:
    session, tenant_id = import_session
    contacts = [Contact(tenant_id=tenant_id, email=f"bulk-{index}@example.com") for index in range(3)]
    session.add_all(contacts)
    session.commit()
    service = ContactService(session, tenant_id)
    removed = session.query(Contact).filter(Contact.email.like("bulk-%")).all()

    report = service.bulk([item.id for item in removed[:2]], "delete", None)
    assert report.affected == 2

    assert session.query(Contact).filter(Contact.email.like("bulk-%")).count() == 1


def test_large_import_is_rejected(import_session) -> None:
    session, tenant_id = import_session
    service = ImportService(session, tenant_id)
    headers = ["email"]
    rows = [{"email": f"person-{index}@example.com"} for index in range(100_001)]
    with pytest.raises(ImportValidationError, match="100,000"):
        service.import_rows(headers, rows)


def test_email_status_caps_unique_dns_lookups_per_import(import_session, monkeypatch) -> None:
    session, tenant_id = import_session
    service = ImportService(session, tenant_id)
    calls = {"n": 0}

    def fake_resolve(_domain, *_args, **_kwargs):
        calls["n"] += 1
        raise dns.exception.DNSException("forced failure")

    monkeypatch.setattr(imports_module.dns.resolver, "resolve", fake_resolve)
    context = service.prepare(["email"], [{"email": f"user{i}@host{i}.test"} for i in range(60)], {"email": "email"}, "SKIP")
    for index in range(60):
        status, _reason = service.email_status(f"user{index}@host{index}.test", context)
        assert status in {"UNKNOWN", "DISPOSABLE"}
    assert calls["n"] == MAX_UNIQUE_DNS_LOOKUPS
