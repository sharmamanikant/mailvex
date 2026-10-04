from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import (
    Base,
    ContactList,
    Tenant,
)
from app.schemas.contacts import (
    ContactCreate,
    ContactFieldDefinitionCreate,
    ContactUpdate,
)
from app.services.contact_fields import ContactFieldService
from app.services.contacts import (
    BULK_STATUS_BLOCKED,
    ContactConflictError,
    ContactNotFoundError,
    ContactService,
)


@pytest.fixture()
def contact_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'contacts.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        yield session, tenant.id
    engine.dispose()


def payload(email: str = "ada@example.com", tag_ids: list | None = None, list_ids: list | None = None) -> ContactCreate:
    return ContactCreate(
        first_name="Ada",
        last_name="Lovelace",
        email=email,
        company="Analytical Engines",
        source="manual",
        custom_fields={"skills": "Python", "availability": "Immediate"},
        tag_ids=tag_ids,
        list_ids=list_ids,
    )


def test_contact_crud_and_custom_fields(contact_session) -> None:
    session, tenant_id = contact_session
    service = ContactService(session, tenant_id)
    contact = service.create(payload())
    assert contact.email == "ada@example.com"
    assert {item.field_key for item in contact.custom_fields} == {"skills", "availability"}

    updated = service.update(contact.id, ContactUpdate(company="Engine Co", custom_fields={"technology": "Python"}))
    assert updated.company == "Engine Co"
    assert updated.custom_fields[0].field_key == "technology"
    service.delete(contact.id)
    with pytest.raises(ContactNotFoundError):
        service.get(contact.id)


def test_search_pagination_and_duplicate_email(contact_session) -> None:
    session, tenant_id = contact_session
    service = ContactService(session, tenant_id)
    service.create(payload())
    service.create(payload("grace@example.com"))
    contacts, total = service.list_contacts(page=1, page_size=1, search="grace", status=None, source=None, sort="email", descending=False)
    assert total == 1
    assert contacts[0].email == "grace@example.com"
    with pytest.raises(ContactConflictError):
        service.create(payload())


def test_contact_lookup_is_tenant_scoped(contact_session) -> None:
    session, tenant_id = contact_session
    contact = ContactService(session, tenant_id).create(payload())
    with pytest.raises(ContactNotFoundError):
        ContactService(session, uuid4()).get(contact.id)


def test_list_can_be_deleted(contact_session) -> None:
    session, tenant_id = contact_session
    item = ContactList(tenant_id=tenant_id, name="Prospects")
    session.add(item)
    session.commit()
    ContactService(session, tenant_id).delete_list(item.id)
    assert session.get(ContactList, item.id) is None


def test_contact_filters_and_sorting(contact_session) -> None:
    session, tenant_id = contact_session
    service = ContactService(session, tenant_id)
    service.create(
        ContactCreate(
            first_name="Ada",
            last_name="Lovelace",
            email="ada@example.com",
            company="Acme",
            source="manual",
            industry="IT",
        )
    )
    service.create(payload("grace@example.com"))
    contacts, total = service.list_contacts(page=1, page_size=10, search=None, status="ACTIVE", source="manual", sort="email", descending=True)
    assert total == 2
    assert [contact.email for contact in contacts] == sorted([contact.email for contact in contacts], reverse=True)
    filtered, filtered_total = service.list_contacts(page=1, page_size=10, search=None, status=None, source=None, industry="IT", sort="email", descending=False)
    assert filtered_total == 1
    assert filtered[0].company == "Acme"


def test_contact_tag_and_list_filters(contact_session) -> None:
    session, tenant_id = contact_session
    service = ContactService(session, tenant_id)
    tag = service.create_tag("VIP")
    contact_list = service.create_list("Prospects", None)
    contact = service.create(payload())
    service.bulk([contact.id], "tag", tag.id)
    service.bulk([contact.id], "list", contact_list.id)
    _, tagged_total = service.list_contacts(page=1, page_size=10, search=None, status=None, source=None, tag="VIP", sort="email", descending=False)
    assert tagged_total == 1
    _, listed_total = service.list_contacts(page=1, page_size=10, search=None, status=None, source=None, list_id=contact_list.id, sort="email", descending=False)
    assert listed_total == 1
    assert service.get(contact.id).email == "ada@example.com"


def test_contact_scoped_to_tenant_in_filters(contact_session) -> None:
    session, tenant_id = contact_session
    service = ContactService(session, tenant_id)
    contact = service.create(payload())
    _list = service.create_list("Mine", None)
    service.add_list_members(_list.id, [contact.id])
    foreign = ContactService(session, uuid4())
    _, foreign_total = foreign.list_contacts(page=1, page_size=10, search=None, status=None, source=None, sort="email", descending=False)
    assert foreign_total == 0
    with pytest.raises(ContactNotFoundError):
        foreign.get_list(_list.id)
    with pytest.raises(ContactNotFoundError):
        foreign.add_list_members(_list.id, [contact.id])


def test_contact_create_with_tag_and_list_ids(contact_session) -> None:
    session, tenant_id = contact_session
    service = ContactService(session, tenant_id)
    tag = service.create_tag("VIP")
    contact_list = service.create_list("Prospects", None)
    contact = service.create(payload(tag_ids=[tag.id], list_ids=[contact_list.id]))
    assert contact
    tag_memberships = {item.contact_tag_id for item in service.get(contact.id).tag_memberships}
    assert tag_memberships == {tag.id}
    list_memberships = {item.contact_list_id for item in service.get(contact.id).list_memberships}
    assert list_memberships == {contact_list.id}
    # Idempotent for both directions.
    service.bulk([contact.id], "untag", tag.id)
    service.bulk([contact.id], "unlist", contact_list.id)
    assert service.get(contact.id).tag_memberships == []
    assert service.get(contact.id).list_memberships == []

    with pytest.raises(ContactNotFoundError):
        service.bulk([contact.id], "tag", uuid4())
    with pytest.raises(ContactNotFoundError):
        service.bulk([contact.id], "list", uuid4())


def test_bulk_status_change_blocks_suppression(contact_session) -> None:
    session, tenant_id = contact_session
    service = ContactService(session, tenant_id)
    contact = service.create(payload())
    with pytest.raises(ContactConflictError):
        service.bulk([contact.id], "status", None, status=next(iter(BULK_STATUS_BLOCKED)))
    report = service.bulk([contact.id], "status", None, status="INACTIVE")
    assert report.affected == 1
    assert service.get(contact.id).status == "INACTIVE"


def test_bulk_skips_missing_contacts(contact_session) -> None:
    session, tenant_id = contact_session
    service = ContactService(session, tenant_id)
    contact = service.create(payload())
    missing = uuid4()
    report = service.bulk([contact.id, missing], "delete", None)
    assert report.affected == 1
    assert report.skipped == 1
    with pytest.raises(ContactNotFoundError):
        service.get(contact.id)


def test_custom_field_validation_uses_definitions(contact_session) -> None:
    session, tenant_id = contact_session
    service = ContactService(session, tenant_id)
    ContactFieldService(session, tenant_id).create(ContactFieldDefinitionCreate(key="experience", label="Experience", field_type="NUMBER"))
    ContactFieldService(session, tenant_id).create(ContactFieldDefinitionCreate(key="availability", label="Availability", field_type="SELECT", options=["Immediate", "Later"]))
    created = service.create(ContactCreate(**{**payload().model_dump(), "custom_fields": {"experience": "5"}}))
    assert created.custom_fields[0].field_value == "5"
    with pytest.raises(ContactConflictError):
        service.create(ContactCreate(**{**payload("bad@example.com").model_dump(), "custom_fields": {"experience": "five"}}))
    with pytest.raises(ContactConflictError):
        service.create(ContactCreate(**{**payload("autre@example.com").model_dump(), "custom_fields": {"unknown": "x"}}))


def test_custom_field_permissive_without_definitions(contact_session) -> None:
    session, tenant_id = contact_session
    service = ContactService(session, tenant_id)
    contact = service.create(payload())
    assert any(item.field_key == "availability" for item in contact.custom_fields)


def test_add_list_members_strict_on_missing_contacts(contact_session) -> None:
    session, tenant_id = contact_session
    service = ContactService(session, tenant_id)
    contact_list = service.create_list("Prospects", None)
    contact = service.create(payload())
    with pytest.raises(ContactNotFoundError):
        service.add_list_members(contact_list.id, [contact.id, uuid4()])
