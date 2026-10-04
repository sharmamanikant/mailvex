from __future__ import annotations

import datetime as dt
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import ContactFieldDefinition
from app.schemas.contacts import (
    ContactFieldDefinitionCreate,
    ContactFieldDefinitionUpdate,
)
from app.services.audit import AuditService


class ContactFieldConflictError(ValueError):
    pass


class ContactFieldNotFoundError(LookupError):
    pass


class ContactFieldService:
    def __init__(self, session: Session, tenant_id: UUID, actor_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id

    @staticmethod
    def validate_value(definition: ContactFieldDefinition, value: str) -> str:
        """Return the canonical, validated string form of a custom-field value."""
        raw = str(value).strip()
        field_type = definition.field_type
        options = definition.options or []
        if field_type == "TEXT":
            return raw
        if field_type == "NUMBER":
            try:
                number = float(raw)
            except (TypeError, ValueError) as exc:
                raise ValueError(f"Field {definition.key} expects a number") from exc
            return str(int(number)) if number.is_integer() else str(number)
        if field_type == "BOOLEAN":
            normalized = raw.lower()
            if normalized in {"true", "1", "yes", "y"}:
                return "true"
            if normalized in {"false", "0", "no", "n"}:
                return "false"
            raise ValueError(f"Field {definition.key} expects true or false")
        if field_type == "DATE":
            try:
                return dt.date.fromisoformat(raw[:10]).isoformat()
            except ValueError as exc:
                raise ValueError(f"Field {definition.key} expects an ISO date (YYYY-MM-DD)") from exc
        if field_type == "SELECT":
            if raw not in options:
                raise ValueError(f"Field {definition.key} expects one of: {', '.join(options)}")
            return raw
        if field_type == "MULTI_SELECT":
            selected = [item.strip() for item in raw.split(",") if item.strip()]
            invalid = [item for item in selected if item not in options]
            if invalid:
                raise ValueError(f"Field {definition.key} contains invalid options: {', '.join(invalid)}")
            return ", ".join(selected)
        return raw

    def definitions_map(self) -> dict[str, ContactFieldDefinition]:
        items = list(self.session.scalars(select(ContactFieldDefinition).where(ContactFieldDefinition.tenant_id == self.tenant_id)).all())
        return {item.key: item for item in items}

    def list_fields(self) -> list[ContactFieldDefinition]:
        return list(self.session.scalars(select(ContactFieldDefinition).where(ContactFieldDefinition.tenant_id == self.tenant_id).order_by(ContactFieldDefinition.key)).all())

    def create(self, payload: ContactFieldDefinitionCreate) -> ContactFieldDefinition:
        item = ContactFieldDefinition(
            tenant_id=self.tenant_id,
            key=payload.key,
            label=payload.label,
            field_type=payload.field_type,
            options=payload.options,
            required=payload.required,
        )
        self.session.add(item)
        try:
            self.session.flush()
            self._audit("CUSTOM_FIELD_DEFINED", "contact_field_definition", item.id, {"key": item.key, "field_type": item.field_type})
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ContactFieldConflictError("A custom field with this key already exists") from exc
        return item

    def get(self, field_id: UUID) -> ContactFieldDefinition:
        item = self.session.scalar(select(ContactFieldDefinition).where(ContactFieldDefinition.id == field_id, ContactFieldDefinition.tenant_id == self.tenant_id))
        if item is None:
            raise ContactFieldNotFoundError("Custom field definition not found")
        return item

    def update(self, field_id: UUID, payload: ContactFieldDefinitionUpdate) -> ContactFieldDefinition:
        item = self.get(field_id)
        values = payload.model_dump(exclude_unset=True)
        for key, value in values.items():
            setattr(item, key, value)
        self._audit("CUSTOM_FIELD_UPDATED", "contact_field_definition", item.id, {"key": item.key})
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ContactFieldConflictError("A custom field with this key already exists") from exc
        return item

    def delete(self, field_id: UUID) -> None:
        item = self.get(field_id)
        self.session.delete(item)
        self._audit("CUSTOM_FIELD_DELETED", "contact_field_definition", field_id, {"key": item.key})
        self.session.commit()

    def _audit(self, action: str, resource_type: str, resource_id: UUID | None, metadata: dict[str, Any] | None = None) -> None:
        AuditService(self.session, self.tenant_id, self.actor_id).record(action, resource_type, resource_id, metadata or {})