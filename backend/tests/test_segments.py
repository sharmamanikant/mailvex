from __future__ import annotations

from uuid import uuid4

import pytest
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

from app.models import Base, Contact, ContactCustomField, Tenant
from app.schemas.segments import (
    ContactSegmentCreate,
    ContactSegmentFilter,
    ContactSegmentUpdate,
    SegmentCondition,
)
from app.services.segments import (
    SegmentConflictError,
    SegmentNotFoundError,
    SegmentService,
)


@pytest.fixture()
def segment_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'segments.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
        session.add(tenant)
        session.flush()
        yield session, tenant.id
    engine.dispose()


def _seed_contacts(session: Session, tenant_id) -> None:
    session.add_all(
        [
            Contact(tenant_id=tenant_id, email="alice@example.com", first_name="Alice", last_name="A", designation="Manager", industry="IT", status="ACTIVE"),
            Contact(tenant_id=tenant_id, email="bob@example.com", first_name="Bob", last_name="B", designation="Engineer", industry="Finance", status="INACTIVE"),
            Contact(tenant_id=tenant_id, email="carol@example.com", first_name="Carol", last_name="C", designation="Manager", industry="IT", status="ACTIVE"),
        ]
    )
    session.flush()
    alice = session.scalar(select(Contact).where(Contact.email == "alice@example.com"))
    assert alice is not None, "missing alice"
    session.add_all(
        [
            ContactCustomField(tenant_id=tenant_id, contact_id=alice.id, field_key="skills", field_value="Python, SQL", field_type="MULTI_SELECT"),
            ContactCustomField(tenant_id=tenant_id, contact_id=alice.id, field_key="experience", field_value="8", field_type="NUMBER"),
        ]
    )
    session.commit()


def _single(conditions: list[dict]) -> dict:
    return {"match": "all", "conditions": conditions}


def test_segment_crud(segment_session) -> None:
    session, tenant_id = segment_session
    service = SegmentService(session, tenant_id)
    segment = service.create(
        ContactSegmentCreate(
            name="IT Managers",
            description="IT managers",
            filters=ContactSegmentFilter(match="all", conditions=[SegmentCondition(field="industry", operator="eq", value="IT")]),
        )
    )
    assert segment.name == "IT Managers"
    assert SegmentService(session, tenant_id).get(segment.id).id == segment.id
    updated = service.update(segment.id, ContactSegmentUpdate(name="Senior IT Managers"))
    assert updated.name == "Senior IT Managers"
    with pytest.raises(SegmentConflictError):
        service.create(
            ContactSegmentCreate(
                name="Senior IT Managers",
                filters=ContactSegmentFilter(match="all", conditions=[SegmentCondition(field="status", operator="eq", value="ACTIVE")]),
            )
        )
    service.delete(segment.id)
    with pytest.raises(SegmentNotFoundError):
        service.get(segment.id)
    # Cross-tenant lookup must not find the segment.
    with pytest.raises(SegmentNotFoundError):
        SegmentService(session, uuid4()).get(segment.id)


def test_segment_evaluates_scalar_fields(segment_session) -> None:
    session, tenant_id = segment_session
    _seed_contacts(session, tenant_id)
    service = SegmentService(session, tenant_id)
    managers = service.create(
        ContactSegmentCreate(
            name="Managers",
            filters=ContactSegmentFilter(match="all", conditions=[SegmentCondition(field="designation", operator="eq", value="Manager")]),
        )
    )
    result = service.evaluate(managers.id)
    assert result.total == 2
    assert {contact.email for contact in result.contacts} == {"alice@example.com", "carol@example.com"}

    inactive = service.create(
        ContactSegmentCreate(
            name="Inactive",
            filters=ContactSegmentFilter(match="all", conditions=[SegmentCondition(field="status", operator="eq", value="inactive")]),
        )
    )
    result = service.evaluate(inactive.id)
    assert result.total == 1
    assert result.contacts[0].email == "bob@example.com"


def test_segment_match_any(segment_session) -> None:
    session, tenant_id = segment_session
    _seed_contacts(session, tenant_id)
    service = SegmentService(session, tenant_id)
    segment = service.create(
        ContactSegmentCreate(
            name="Managers or Finance",
            filters=ContactSegmentFilter(
                match="any",
                conditions=[
                    SegmentCondition(field="designation", operator="eq", value="Manager"),
                    SegmentCondition(field="industry", operator="eq", value="Finance"),
                ],
            ),
        )
    )
    result = service.evaluate(segment.id)
    assert {contact.email for contact in result.contacts} == {"alice@example.com", "bob@example.com", "carol@example.com"}


def test_segment_contains_and_not_contains(segment_session) -> None:
    session, tenant_id = segment_session
    _seed_contacts(session, tenant_id)
    service = SegmentService(session, tenant_id)
    engineers = service.create(
        ContactSegmentCreate(
            name="Engineers",
            filters=ContactSegmentFilter(match="all", conditions=[SegmentCondition(field="designation", operator="contains", value="engine")]),
        )
    )
    assert service.evaluate(engineers.id).total == 1
    not_it = service.create(
        ContactSegmentCreate(
            name="Not IT",
            filters=ContactSegmentFilter(match="all", conditions=[SegmentCondition(field="industry", operator="not_contains", value="IT")]),
        )
    )
    assert {contact.email for contact in service.evaluate(not_it.id).contacts} == {"bob@example.com"}


def test_segment_custom_fields(segment_session) -> None:
    session, tenant_id = segment_session
    _seed_contacts(session, tenant_id)
    service = SegmentService(session, tenant_id)
    skilly = service.create(
        ContactSegmentCreate(
            name="Skilled",
            filters=ContactSegmentFilter(match="all", conditions=[SegmentCondition(field="custom.skills", operator="contains", value="Python")]),
        )
    )
    result = service.evaluate(skilly.id)
    assert result.total == 1
    assert result.contacts[0].email == "alice@example.com"

    experienced = service.create(
        ContactSegmentCreate(
            name="Experienced",
            filters=ContactSegmentFilter(match="all", conditions=[SegmentCondition(field="custom.experience", operator="gte", value=5)]),
        )
    )
    assert service.evaluate(experienced.id).total == 1


def test_segment_in_and_empty(segment_session) -> None:
    session, tenant_id = segment_session
    _seed_contacts(session, tenant_id)
    service = SegmentService(session, tenant_id)
    in_item = service.create(
        ContactSegmentCreate(
            name="In list",
            filters=ContactSegmentFilter(match="all", conditions=[SegmentCondition(field="industry", operator="in", value=["IT", "Energy"])]),
        )
    )
    assert {contact.email for contact in service.evaluate(in_item.id).contacts} == {"alice@example.com", "carol@example.com"}

    missing_designation = service.create(
        ContactSegmentCreate(
            name="No designation",
            filters=ContactSegmentFilter(match="all", conditions=[SegmentCondition(field="designation", operator="is_empty", value=None)]),
        )
    )
    # All seeded contacts have a designation, so nothing matches.
    assert service.evaluate(missing_designation.id).total == 0


def test_segment_pagination(segment_session) -> None:
    session, tenant_id = segment_session
    _seed_contacts(session, tenant_id)
    service = SegmentService(session, tenant_id)
    all_contacts = service.create(
        ContactSegmentCreate(
            name="Everyone",
            filters=ContactSegmentFilter(match="all", conditions=[SegmentCondition(field="status", operator="is_not_empty", value=None)]),
        )
    )
    result = service.evaluate(all_contacts.id, page=1, page_size=2)
    assert result.total == 3
    assert len(result.contacts) == 2
    assert service.evaluate(all_contacts.id, page=2, page_size=2).total == 3


def test_invalid_condition_operators_are_rejected() -> None:
    with pytest.raises(ValueError):
        SegmentCondition(field="designation", operator="explodes", value="Manager")
    with pytest.raises(ValueError):
        SegmentCondition(field="unknown.field", operator="eq", value="x")