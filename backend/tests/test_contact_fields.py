from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Tenant
from app.schemas.contacts import (
    ContactFieldDefinitionCreate,
    ContactFieldDefinitionUpdate,
)
from app.services.contact_fields import (
    ContactFieldConflictError,
    ContactFieldNotFoundError,
    ContactFieldService,
)


@pytest.fixture()
def field_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fields.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        yield session, tenant.id
    engine.dispose()


def test_create_and_list_field_definitions(field_session) -> None:
    session, tenant_id = field_session
    service = ContactFieldService(session, tenant_id)
    item = service.create(
        ContactFieldDefinitionCreate(key="industry", label="Industry", field_type="SELECT", options=["IT", "Finance"])
    )
    assert item.key == "industry"
    assert item.field_type == "SELECT"
    assert item.options == ["IT", "Finance"]
    assert [candidate.key for candidate in service.list_fields()] == ["industry"]


def test_duplicate_key_is_conflict(field_session) -> None:
    session, tenant_id = field_session
    service = ContactFieldService(session, tenant_id)
    service.create(ContactFieldDefinitionCreate(key="skills", label="Skills", field_type="TEXT"))
    with pytest.raises(ContactFieldConflictError):
        service.create(ContactFieldDefinitionCreate(key="skills", label="Skills 2", field_type="TEXT"))
    # The same key is allowed in a different tenant.
    other = ContactFieldService(session, uuid4())
    other.create(ContactFieldDefinitionCreate(key="skills", label="Skills", field_type="TEXT"))


def test_get_update_delete(field_session) -> None:
    session, tenant_id = field_session
    service = ContactFieldService(session, tenant_id)
    item = service.create(ContactFieldDefinitionCreate(key="budget", label="Budget", field_type="NUMBER"))
    fetched = service.get(item.id)
    assert fetched.key == "budget"
    updated = service.update(item.id, ContactFieldDefinitionUpdate(label="Annual Budget", required=True))
    assert updated.label == "Annual Budget"
    assert updated.required is True
    with pytest.raises(ContactFieldNotFoundError):
        ContactFieldService(session, uuid4()).get(item.id)  # other tenant must not find it
    service.delete(item.id)
    with pytest.raises(ContactFieldNotFoundError):
        service.get(item.id)


def test_value_validation_coerces_types(field_session) -> None:
    session, tenant_id = field_session
    service = ContactFieldService(session, tenant_id)
    definitions = {
        "text": service.create(ContactFieldDefinitionCreate(key="text", label="Text", field_type="TEXT")),
        "number": service.create(ContactFieldDefinitionCreate(key="number", label="Number", field_type="NUMBER")),
        "boolean": service.create(ContactFieldDefinitionCreate(key="boolean", label="Boolean", field_type="BOOLEAN")),
        "date": service.create(ContactFieldDefinitionCreate(key="date", label="Date", field_type="DATE")),
        "select": service.create(ContactFieldDefinitionCreate(key="select", label="Select", field_type="SELECT", options=["Immediate", "Later"])),
        "multi": service.create(ContactFieldDefinitionCreate(key="multi", label="Multi", field_type="MULTI_SELECT", options=["Python", "Java"])),
    }
    assert ContactFieldService.validate_value(definitions["text"], "  Hello  ") == "Hello"
    assert ContactFieldService.validate_value(definitions["number"], "5") == "5"
    assert ContactFieldService.validate_value(definitions["number"], "5.0") == "5"
    assert ContactFieldService.validate_value(definitions["boolean"], "YES") == "true"
    assert ContactFieldService.validate_value(definitions["boolean"], "0") == "false"
    assert ContactFieldService.validate_value(definitions["date"], "2026-08-27T10:00:00") == "2026-08-27"
    assert ContactFieldService.validate_value(definitions["select"], "Immediate") == "Immediate"
    assert ContactFieldService.validate_value(definitions["multi"], "Java, Python") == "Java, Python"

    with pytest.raises(ValueError):
        ContactFieldService.validate_value(definitions["number"], "abc")
    with pytest.raises(ValueError):
        ContactFieldService.validate_value(definitions["boolean"], "maybe")
    with pytest.raises(ValueError):
        ContactFieldService.validate_value(definitions["date"], "27/08/2026")
    with pytest.raises(ValueError):
        ContactFieldService.validate_value(definitions["select"], "Soon")
    with pytest.raises(ValueError):
        ContactFieldService.validate_value(definitions["multi"], "C++")


def test_select_fields_require_options(field_session) -> None:
    session, tenant_id = field_session
    _ = ContactFieldService(session, tenant_id)
    with pytest.raises(ValueError):
        ContactFieldDefinitionCreate(key="pick", label="Pick", field_type="SELECT")
    with pytest.raises(ValueError):
        ContactFieldDefinitionCreate(key="pick", label="Pick", field_type="TEXT", options=["A", "B"])