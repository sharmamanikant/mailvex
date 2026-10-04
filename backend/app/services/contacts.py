from __future__ import annotations

from dataclasses import dataclass
from typing import Any, ClassVar
from uuid import UUID

from sqlalchemy import delete, func, or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session, selectinload

from app.billing import EVENT_CONTACT_CREATED, UsageService
from app.models import (
    Contact,
    ContactCustomField,
    ContactList,
    ContactListMember,
    ContactTag,
    ContactTagMember,
)
from app.schemas.contacts import ContactCreate, ContactUpdate
from app.services.audit import AuditService
from app.services.contact_fields import ContactFieldService
from app.utils.emails import normalize_email

BULK_STATUS_BLOCKED = {"UNSUBSCRIBED", "BOUNCED", "SUPPRESSED"}


class ContactConflictError(ValueError):
    pass


class ContactNotFoundError(LookupError):
    pass


@dataclass
class BulkReport:
    affected: int = 0
    skipped: int = 0
    failed: int = 0


class ContactService:
    SORT_FIELDS: ClassVar[dict[str, Any]] = {
        "created_at": Contact.created_at,
        "updated_at": Contact.updated_at,
        "email": Contact.email,
        "company": Contact.company,
        "designation": Contact.designation,
        "location": Contact.location,
        "status": Contact.status,
        "verification_status": Contact.verification_status,
        "verification_score": Contact.verification_score,
        "risk_level": Contact.risk_level,
    }

    def __init__(self, session: Session, tenant_id: UUID, actor_id: UUID | None = None) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id

    def _contact(self, contact_id: UUID) -> Contact:
        contact = self.session.scalar(
            select(Contact)
            .options(selectinload(Contact.custom_fields), selectinload(Contact.list_memberships), selectinload(Contact.tag_memberships))
            .where(Contact.id == contact_id, Contact.tenant_id == self.tenant_id)
        )
        if contact is None:
            raise ContactNotFoundError("Contact not found")
        return contact

    def create(self, payload: ContactCreate) -> Contact:
        values = payload.model_dump(exclude={"custom_fields", "tag_ids", "list_ids"})
        values["email"] = normalize_email(values["email"])
        if values.get("status"):
            values["status"] = values["status"].upper()
        UsageService(self.session, self.tenant_id, self.actor_id).meter(
            EVENT_CONTACT_CREATED,
            resource_type="contact",
            metadata={"email": values["email"]},
        )
        contact = Contact(tenant_id=self.tenant_id, **values)
        contact.custom_fields = [ContactCustomField(tenant_id=self.tenant_id, field_key=key, field_value=value) for key, value in self._validated_custom_fields(payload.custom_fields or {}).items()]
        self.session.add(contact)
        try:
            self.session.flush()
            self._assign_memberships(contact, payload.tag_ids, payload.list_ids)
            self._audit("CONTACT_CREATED", "contact", contact.id)
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ContactConflictError("A contact with this email already exists") from exc
        return self._contact(contact.id)

    def get(self, contact_id: UUID) -> Contact:
        return self._contact(contact_id)

    def duplicate_groups(self) -> list[list[Contact]]:
        contacts = list(self.session.scalars(select(Contact).options(selectinload(Contact.custom_fields)).where(Contact.tenant_id == self.tenant_id)).all())
        groups: dict[tuple[str, str], list[Contact]] = {}
        for contact in contacts:
            name = f"{contact.first_name or ''} {contact.last_name or ''}".strip().lower()
            company = (contact.company or '').strip().lower()
            if name and company:
                groups.setdefault((name, company), []).append(contact)
        return [group for group in groups.values() if len(group) > 1]

    def merge(self, source_id: UUID, target_id: UUID) -> Contact:
        if source_id == target_id:
            raise ContactNotFoundError("Choose two different contacts")
        source = self._contact(source_id)
        target = self._contact(target_id)
        for field in ("first_name", "last_name", "phone", "company", "designation", "location", "website", "industry"):
            if not getattr(target, field) and getattr(source, field):
                setattr(target, field, getattr(source, field))
        existing_fields = {item.field_key for item in target.custom_fields}
        target.custom_fields.extend(ContactCustomField(tenant_id=self.tenant_id, field_key=item.field_key, field_value=item.field_value) for item in source.custom_fields if item.field_key not in existing_fields)
        self.session.delete(source)
        self.session.commit()
        return self._contact(target.id)

    def update(self, contact_id: UUID, payload: ContactUpdate) -> Contact:
        contact = self._contact(contact_id)
        values = payload.model_dump(exclude_unset=True, exclude={"custom_fields", "tag_ids", "list_ids"})
        for key, value in values.items():
            setattr(contact, key, value)
        if contact.email:
            contact.email = normalize_email(contact.email)
        if payload.custom_fields is not None:
            contact.custom_fields.clear()
            contact.custom_fields.extend(ContactCustomField(tenant_id=self.tenant_id, field_key=key, field_value=value) for key, value in self._validated_custom_fields(payload.custom_fields).items())
        if payload.tag_ids is not None or payload.list_ids is not None:
            self._assign_memberships(contact, payload.tag_ids, payload.list_ids)
        self._audit("CONTACT_UPDATED", "contact", contact.id)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ContactConflictError("A contact with this email already exists") from exc
        return self._contact(contact.id)

    def delete(self, contact_id: UUID) -> None:
        contact = self._contact(contact_id)
        self.session.delete(contact)
        self._audit("CONTACT_DELETED", "contact", contact_id)
        self.session.commit()

    def list_contacts(self, *, page: int, page_size: int, search: str | None, status: str | None, source: str | None, industry: str | None = None, tag: str | None = None, list_id: UUID | None = None, sort: str, descending: bool, verification_status: str | None = None, risk_level: str | None = None) -> tuple[list[Contact], int]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        filters = [Contact.tenant_id == self.tenant_id]
        if search:
            term = f"%{search.strip()}%"
            filters.append(or_(Contact.email.ilike(term), Contact.first_name.ilike(term), Contact.last_name.ilike(term), Contact.company.ilike(term), Contact.designation.ilike(term), Contact.location.ilike(term)))
        if status:
            filters.append(Contact.status == status)
        if source:
            filters.append(Contact.source == source)
        if industry:
            filters.append(Contact.industry == industry)
        if verification_status:
            filters.append(Contact.verification_status == verification_status)
        if risk_level:
            filters.append(Contact.risk_level == risk_level)
        if tag:
            filters.append(Contact.id.in_(select(ContactTagMember.contact_id).join(ContactTag, ContactTag.id == ContactTagMember.contact_tag_id).where(ContactTagMember.tenant_id == self.tenant_id, ContactTag.name == tag)))
        if list_id:
            filters.append(Contact.id.in_(select(ContactListMember.contact_id).where(ContactListMember.tenant_id == self.tenant_id, ContactListMember.contact_list_id == list_id)))
        total = self.session.scalar(select(func.count(Contact.id)).where(*filters)) or 0
        order_column = self.SORT_FIELDS.get(sort, Contact.created_at)
        order = order_column.desc() if descending else order_column.asc()
        contacts = list(self.session.scalars(select(Contact).options(selectinload(Contact.custom_fields), selectinload(Contact.list_memberships), selectinload(Contact.tag_memberships)).where(*filters).order_by(order, Contact.id).offset((page - 1) * page_size).limit(page_size)).all())
        return contacts, total

    def create_list(self, name: str, description: str | None) -> ContactList:
        item = ContactList(tenant_id=self.tenant_id, name=name.strip(), description=description)
        self.session.add(item)
        try:
            self.session.flush()
            self._audit("CONTACT_LIST_CREATED", "contact_list", item.id)
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ContactConflictError("A list with this name already exists") from exc
        return item

    def lists(self) -> list[ContactList]:
        return list(self.session.scalars(select(ContactList).where(ContactList.tenant_id == self.tenant_id).order_by(ContactList.name)).all())

    def get_list(self, list_id: UUID) -> ContactList:
        item = self.session.scalar(select(ContactList).options(selectinload(ContactList.members)).where(ContactList.id == list_id, ContactList.tenant_id == self.tenant_id))
        if item is None:
            raise ContactNotFoundError("List not found")
        return item

    def update_list(self, list_id: UUID, name: str | None, description: str | None) -> ContactList:
        item = self.get_list(list_id)
        if name is not None:
            item.name = name.strip()
        if description is not None:
            item.description = description
        self._audit("CONTACT_LIST_UPDATED", "contact_list", item.id)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ContactConflictError("A list with this name already exists") from exc
        return self.get_list(list_id)

    def add_list_members(self, list_id: UUID, contact_ids: list[UUID]) -> int:
        self.get_list(list_id)
        existing_ids = self._existing_contact_ids(contact_ids)
        if len(existing_ids) != len(set(contact_ids)):
            raise ContactNotFoundError("One or more contacts were not found in this tenant")
        memberships = set(self.session.scalars(select(ContactListMember.contact_id).where(ContactListMember.tenant_id == self.tenant_id, ContactListMember.contact_list_id == list_id, ContactListMember.contact_id.in_(existing_ids))).all())
        added = existing_ids - memberships
        self.session.add_all(ContactListMember(tenant_id=self.tenant_id, contact_list_id=list_id, contact_id=contact_id) for contact_id in added)
        self._audit("CONTACT_ADDED_TO_LIST", "contact_list", list_id, {"count": len(added)})
        self.session.commit()
        return len(added)

    def remove_list_member(self, list_id: UUID, contact_id: UUID) -> None:
        self.get_list(list_id)
        membership = self.session.scalar(select(ContactListMember).where(ContactListMember.tenant_id == self.tenant_id, ContactListMember.contact_list_id == list_id, ContactListMember.contact_id == contact_id))
        if membership is None:
            raise ContactNotFoundError("List membership not found")
        self.session.delete(membership)
        self._audit("CONTACT_REMOVED_FROM_LIST", "contact_list", list_id)
        self.session.commit()

    def delete_list(self, list_id: UUID) -> None:
        item = self.session.scalar(select(ContactList).where(ContactList.id == list_id, ContactList.tenant_id == self.tenant_id))
        if item is None:
            raise ContactNotFoundError("List not found")
        self.session.delete(item)
        self._audit("CONTACT_LIST_DELETED", "contact_list", list_id)
        self.session.commit()

    def create_tag(self, name: str) -> ContactTag:
        item = ContactTag(tenant_id=self.tenant_id, name=name.strip())
        self.session.add(item)
        try:
            self.session.flush()
            self._audit("TAG_CREATED", "contact_tag", item.id)
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ContactConflictError("A tag with this name already exists") from exc
        return item

    def tags(self) -> list[ContactTag]:
        return list(self.session.scalars(select(ContactTag).where(ContactTag.tenant_id == self.tenant_id).order_by(ContactTag.name)).all())

    def update_tag(self, tag_id: UUID, name: str) -> ContactTag:
        item = self.session.scalar(select(ContactTag).where(ContactTag.id == tag_id, ContactTag.tenant_id == self.tenant_id))
        if item is None:
            raise ContactNotFoundError("Tag not found")
        item.name = name.strip()
        self._audit("TAG_UPDATED", "contact_tag", tag_id)
        try:
            self.session.commit()
        except IntegrityError as exc:
            self.session.rollback()
            raise ContactConflictError("A tag with this name already exists") from exc
        return item

    def delete_tag(self, tag_id: UUID) -> None:
        item = self.session.scalar(select(ContactTag).where(ContactTag.id == tag_id, ContactTag.tenant_id == self.tenant_id))
        if item is None:
            raise ContactNotFoundError("Tag not found")
        self.session.delete(item)
        self._audit("TAG_DELETED", "contact_tag", tag_id)
        self.session.commit()

    def bulk(self, contact_ids: list[UUID], action: str, target_id: UUID | None, status: str | None = None) -> BulkReport:
        existing_ids = self._existing_contact_ids(contact_ids)
        skipped = len(set(contact_ids)) - len(existing_ids)
        if not existing_ids:
            return BulkReport(affected=0, skipped=skipped, failed=0)
        ids = list(existing_ids)
        if action == "delete":
            self.session.execute(delete(Contact).where(Contact.tenant_id == self.tenant_id, Contact.id.in_(ids)))
            self._audit("CONTACT_BULK_DELETED", "contact", None, {"count": len(ids)})
        elif action == "tag":
            self._ensure_target(ContactTag, target_id)
            existing = set(self.session.scalars(select(ContactTagMember.contact_id).where(ContactTagMember.tenant_id == self.tenant_id, ContactTagMember.contact_tag_id == target_id, ContactTagMember.contact_id.in_(ids))).all())
            self.session.add_all(ContactTagMember(tenant_id=self.tenant_id, contact_tag_id=target_id, contact_id=contact_id) for contact_id in ids if contact_id not in existing)
        elif action == "untag":
            self._ensure_target(ContactTag, target_id)
            self.session.execute(delete(ContactTagMember).where(ContactTagMember.tenant_id == self.tenant_id, ContactTagMember.contact_tag_id == target_id, ContactTagMember.contact_id.in_(ids)))
        elif action == "list":
            self._ensure_target(ContactList, target_id)
            existing = set(self.session.scalars(select(ContactListMember.contact_id).where(ContactListMember.tenant_id == self.tenant_id, ContactListMember.contact_list_id == target_id, ContactListMember.contact_id.in_(ids))).all())
            self.session.add_all(ContactListMember(tenant_id=self.tenant_id, contact_list_id=target_id, contact_id=contact_id) for contact_id in ids if contact_id not in existing)
        elif action == "unlist":
            self._ensure_target(ContactList, target_id)
            self.session.execute(delete(ContactListMember).where(ContactListMember.tenant_id == self.tenant_id, ContactListMember.contact_list_id == target_id, ContactListMember.contact_id.in_(ids)))
        elif action == "status":
            if status is None:
                raise ContactConflictError("A status is required")
            if status in BULK_STATUS_BLOCKED:
                raise ContactConflictError("Bulk status changes cannot set suppression states; use the compliance and suppression flows")
            self.session.query(Contact).filter(Contact.tenant_id == self.tenant_id, Contact.id.in_(ids)).update({Contact.status: status}, synchronize_session=False)
        if action != "delete":
            self._audit("CONTACT_BULK_UPDATED", "contact", None, {"count": len(ids), "action": action, "target_id": str(target_id) if target_id else None})
        self.session.commit()
        return BulkReport(affected=len(ids), skipped=skipped, failed=0)

    def _audit(self, action: str, resource_type: str, resource_id: UUID | None, metadata: dict[str, Any] | None = None) -> None:
        AuditService(self.session, self.tenant_id, self.actor_id).record(action, resource_type, resource_id, metadata)

    def _ensure_target(self, model: type[ContactTag] | type[ContactList], target_id: UUID | None) -> None:
        if target_id is None or self.session.scalar(select(model.id).where(model.id == target_id, model.tenant_id == self.tenant_id)) is None:
            raise ContactNotFoundError("Target list or tag not found")

    def _existing_contact_ids(self, contact_ids: list[UUID]) -> set[UUID]:
        contacts = self.session.scalars(select(Contact.id).where(Contact.tenant_id == self.tenant_id, Contact.id.in_(contact_ids))).all()
        return set(contacts)

    def _validated_custom_fields(self, values: dict[str, str]) -> dict[str, str]:
        definitions = ContactFieldService(self.session, self.tenant_id).definitions_map()
        result: dict[str, str] = {}
        for key, value in values.items():
            definition = definitions.get(key)
            if definition is None:
                if definitions:
                    raise ContactConflictError(f"Unknown custom field: {key}")
                result[key] = str(value)
                continue
            try:
                result[key] = ContactFieldService.validate_value(definition, value)
            except ValueError as exc:
                raise ContactConflictError(str(exc)) from None
        return result

    def _assign_memberships(self, contact: Contact, tag_ids: list[UUID] | None, list_ids: list[UUID] | None) -> None:
        if tag_ids:
            tags = set(self.session.scalars(select(ContactTag.id).where(ContactTag.tenant_id == self.tenant_id, ContactTag.id.in_(tag_ids))).all())
            unknown = set(tag_ids) - tags
            if unknown:
                raise ContactConflictError("One or more tags were not found in this tenant")
            existing = {item.contact_tag_id for item in contact.tag_memberships}
            for tag_id in tags - existing:
                self.session.add(ContactTagMember(tenant_id=self.tenant_id, contact_tag_id=tag_id, contact_id=contact.id))
        if list_ids:
            lists = set(self.session.scalars(select(ContactList.id).where(ContactList.tenant_id == self.tenant_id, ContactList.id.in_(list_ids))).all())
            unknown = set(list_ids) - lists
            if unknown:
                raise ContactConflictError("One or more lists were not found in this tenant")
            existing = {item.contact_list_id for item in contact.list_memberships}
            for list_id in lists - existing:
                self.session.add(ContactListMember(tenant_id=self.tenant_id, contact_list_id=list_id, contact_id=contact.id))