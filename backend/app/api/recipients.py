"""Canonical recipient list and tag endpoints.

The legacy /contacts/lists and /contacts/tags routes remain available for
existing clients; these routes match the public Contact Management contract.
"""

from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.contacts import serialize_contact
from app.core.database import get_db
from app.schemas.contacts import (
    ContactListCreate,
    ContactListResponse,
    ContactListUpdate,
    ContactMembershipRequest,
    ContactPage,
    ContactTagCreate,
    ContactTagResponse,
    ContactTagUpdate,
)
from app.security.permissions import TenantPrincipal, require_permission
from app.services.contacts import (
    ContactConflictError,
    ContactNotFoundError,
    ContactService,
)

lists_router = APIRouter(prefix="/contact-lists", tags=["contact-lists"])
tags_router = APIRouter(prefix="/tags", tags=["contact-tags"])


def _service(session: Session, principal: TenantPrincipal) -> ContactService:
    return ContactService(session, principal.tenant_id, principal.user_id)


def _error(exc: Exception) -> NoReturn:
    if isinstance(exc, ContactNotFoundError):
        raise HTTPException(status_code=404, detail=str(exc)) from None
    if isinstance(exc, ContactConflictError):
        raise HTTPException(status_code=409, detail=str(exc)) from None
    raise exc


@lists_router.get("", response_model=list[ContactListResponse])
def list_contact_lists(principal: TenantPrincipal = Depends(require_permission("contacts.read")), session: Session = Depends(get_db)) -> list[ContactListResponse]:
    return [ContactListResponse.model_validate(item) for item in _service(session, principal).lists()]


@lists_router.post("", response_model=ContactListResponse, status_code=status.HTTP_201_CREATED)
def create_contact_list(payload: ContactListCreate, principal: TenantPrincipal = Depends(require_permission("contacts.create")), session: Session = Depends(get_db)) -> ContactListResponse:
    try:
        return ContactListResponse.model_validate(_service(session, principal).create_list(payload.name, payload.description))
    except (ContactConflictError, ContactNotFoundError) as exc:
        _error(exc)


@lists_router.get("/{list_id}", response_model=ContactListResponse)
def get_contact_list(list_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.read")), session: Session = Depends(get_db)) -> ContactListResponse:
    try:
        return ContactListResponse.model_validate(_service(session, principal).get_list(list_id))
    except ContactNotFoundError as exc:
        _error(exc)


@lists_router.patch("/{list_id}", response_model=ContactListResponse)
def update_contact_list(list_id: UUID, payload: ContactListUpdate, principal: TenantPrincipal = Depends(require_permission("contacts.update")), session: Session = Depends(get_db)) -> ContactListResponse:
    try:
        return ContactListResponse.model_validate(_service(session, principal).update_list(list_id, payload.name, payload.description))
    except (ContactConflictError, ContactNotFoundError) as exc:
        _error(exc)


@lists_router.delete("/{list_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_contact_list(list_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.delete")), session: Session = Depends(get_db)) -> Response:
    try:
        _service(session, principal).delete_list(list_id)
    except ContactNotFoundError as exc:
        _error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@lists_router.get("/{list_id}/contacts", response_model=ContactPage)
def list_contacts_in_list(
    list_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    search: str | None = Query(None, max_length=200),
    principal: TenantPrincipal = Depends(require_permission("contacts.read")),
    session: Session = Depends(get_db),
) -> ContactPage:
    try:
        _service(session, principal).get_list(list_id)
        contacts, total = _service(session, principal).list_contacts(page=page, page_size=page_size, search=search, status=None, source=None, tag=None, list_id=list_id, sort="created_at", descending=True)
    except ContactNotFoundError as exc:
        _error(exc)
    return ContactPage(items=[serialize_contact(item) for item in contacts], page=page, page_size=page_size, total=total)


@lists_router.post("/{list_id}/members")
def add_contact_list_members(list_id: UUID, payload: ContactMembershipRequest, principal: TenantPrincipal = Depends(require_permission("contacts.update")), session: Session = Depends(get_db)) -> dict[str, int]:
    try:
        return {"added": _service(session, principal).add_list_members(list_id, payload.contact_ids)}
    except ContactNotFoundError as exc:
        _error(exc)


@lists_router.delete("/{list_id}/members/{contact_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_contact_list_member(list_id: UUID, contact_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.update")), session: Session = Depends(get_db)) -> Response:
    try:
        _service(session, principal).remove_list_member(list_id, contact_id)
    except ContactNotFoundError as exc:
        _error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@tags_router.get("", response_model=list[ContactTagResponse])
def list_tags(principal: TenantPrincipal = Depends(require_permission("contacts.read")), session: Session = Depends(get_db)) -> list[ContactTagResponse]:
    return [ContactTagResponse.model_validate(item) for item in _service(session, principal).tags()]


@tags_router.post("", response_model=ContactTagResponse, status_code=status.HTTP_201_CREATED)
def create_tag(payload: ContactTagCreate, principal: TenantPrincipal = Depends(require_permission("contacts.create")), session: Session = Depends(get_db)) -> ContactTagResponse:
    try:
        return ContactTagResponse.model_validate(_service(session, principal).create_tag(payload.name))
    except ContactConflictError as exc:
        _error(exc)


@tags_router.patch("/{tag_id}", response_model=ContactTagResponse)
def update_tag(tag_id: UUID, payload: ContactTagUpdate, principal: TenantPrincipal = Depends(require_permission("contacts.update")), session: Session = Depends(get_db)) -> ContactTagResponse:
    try:
        return ContactTagResponse.model_validate(_service(session, principal).update_tag(tag_id, payload.name))
    except (ContactConflictError, ContactNotFoundError) as exc:
        _error(exc)


@tags_router.delete("/{tag_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_tag(tag_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.delete")), session: Session = Depends(get_db)) -> Response:
    try:
        _service(session, principal).delete_tag(tag_id)
    except ContactNotFoundError as exc:
        _error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)
