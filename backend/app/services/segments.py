from __future__ import annotations

from dataclasses import dataclass
from typing import Any
from uuid import UUID

from sqlalchemy import Numeric, String, and_, cast, exists, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.models import Contact, ContactCustomField, ContactSegment
from app.schemas.segments import (
    ContactSegmentCreate,
    ContactSegmentFilter,
    ContactSegmentUpdate,
    SegmentCondition,
)
from app.services.audit import AuditService

SCALAR_FIELDS: dict[str, Any] = {
    "first_name": Contact.first_name,
    "last_name": Contact.last_name,
    "email": Contact.email,
    "phone": Contact.phone,
    "company": Contact.company,
    "designation": Contact.designation,
    "location": Contact.location,
    "website": Contact.website,
    "industry": Contact.industry,
    "source": Contact.source,
    "source_reference": Contact.source_reference,
    "status": Contact.status,
}


class SegmentConflictError(ValueError):
    pass


class SegmentNotFoundError(LookupError):
    pass


@dataclass
class SegmentResult:
    contacts: list[Contact]
    total: int


class SegmentService:
    def __init__(self, session: Session, tenant_id: UUID, actor_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id

    def list_segments(self) -> list[ContactSegment]:
        return list(self.session.scalars(select(ContactSegment).where(ContactSegment.tenant_id == self.tenant_id).order_by(ContactSegment.name)).all())

    def create(self, payload: ContactSegmentCreate) -> ContactSegment:
        item = ContactSegment(tenant_id=self.tenant_id, name=payload.name.strip(), description=payload.description, filters=payload.filters.model_dump(mode="json"))
        self.session.add(item)
        try:
            self.session.flush()
            self._audit("SEGMENT_CREATED", "contact_segment", item.id, {"name": item.name})
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise SegmentConflictError("A segment with this name already exists") from exc
        return item

    def get(self, segment_id: UUID) -> ContactSegment:
        item = self.session.scalar(select(ContactSegment).where(ContactSegment.id == segment_id, ContactSegment.tenant_id == self.tenant_id))
        if item is None:
            raise SegmentNotFoundError("Segment not found")
        return item

    def update(self, segment_id: UUID, payload: ContactSegmentUpdate) -> ContactSegment:
        item = self.get(segment_id)
        if payload.name is not None:
            item.name = payload.name.strip()
        if payload.description is not None:
            item.description = payload.description
        if payload.filters is not None:
            item.filters = payload.filters.model_dump(mode="json")
        self._audit("SEGMENT_UPDATED", "contact_segment", segment_id, {"name": item.name})
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise SegmentConflictError("A segment with this name already exists") from exc
        return item

    def delete(self, segment_id: UUID) -> None:
        item = self.get(segment_id)
        self.session.delete(item)
        self._audit("SEGMENT_DELETED", "contact_segment", segment_id, {"name": item.name})
        self.session.commit()

    def evaluate(self, segment_id: UUID, *, page: int = 1, page_size: int = 50) -> SegmentResult:
        item = self.get(segment_id)
        filter_payload = ContactSegmentFilter.model_validate(item.filters)
        clause = self._build_clause(filter_payload)
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        base = and_(Contact.tenant_id == self.tenant_id, clause)
        total = self.session.scalar(select(func.count(Contact.id)).where(base)) or 0
        contacts = list(
            self.session.scalars(
                select(Contact)
                .options(
                    selectinload(Contact.custom_fields),
                    selectinload(Contact.list_memberships),
                    selectinload(Contact.tag_memberships),
                )
                .where(base)
                .order_by(Contact.created_at.desc(), Contact.id)
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
        )
        return SegmentResult(contacts=contacts, total=total)

    def _build_clause(self, filters: ContactSegmentFilter) -> Any:
        clauses = [self._condition(condition) for condition in filters.conditions]
        return and_(*clauses) if filters.match == "all" else or_(*clauses)

    def _condition(self, condition: SegmentCondition) -> Any:
        if condition.field.startswith("custom."):
            key = condition.field[len("custom."):]
            custom_field = ContactCustomField.field_value
            return exists().where(
                and_(
                    ContactCustomField.tenant_id == self.tenant_id,
                    ContactCustomField.contact_id == Contact.id,
                    ContactCustomField.field_key == key,
                    self._operator(custom_field, condition),
                )
            )
        column = SCALAR_FIELDS[condition.field]
        return self._operator(column, condition)

    def _operator(self, expression: Any, condition: SegmentCondition) -> Any:
        operator = condition.operator
        value = condition.value
        if operator in {"gt", "gte", "lt", "lte"}:
            numeric = cast(expression, Numeric)
            return {
                "gt": numeric > value,
                "gte": numeric >= value,
                "lt": numeric < value,
                "lte": numeric <= value,
            }[operator]
        if operator == "eq":
            return func.lower(expression) == str(value).lower()
        if operator == "neq":
            return func.lower(expression) != str(value).lower()
        if operator == "contains":
            return cast(expression, String).icontains(str(value))
        if operator == "not_contains":
            return ~cast(expression, String).icontains(str(value))
        if operator == "in":
            if not isinstance(value, list):
                raise AssertionError("Operator 'in' requires a list value")
            return func.lower(expression).in_([str(item).lower() for item in value])
        if operator == "not_in":
            if not isinstance(value, list):
                raise AssertionError("Operator 'not_in' requires a list value")
            return ~func.lower(expression).in_([str(item).lower() for item in value])
        if operator == "is_empty":
            return (expression.is_(None)) | (expression == "")
        if operator == "is_not_empty":
            return (expression.is_not(None)) & (expression != "")
        raise AssertionError(f"Unreachable operator: {operator}")

    def _audit(self, action: str, resource_type: str, resource_id: UUID | None, metadata: dict[str, Any] | None = None) -> None:
        AuditService(self.session, self.tenant_id, self.actor_id).record(action, resource_type, resource_id, metadata)