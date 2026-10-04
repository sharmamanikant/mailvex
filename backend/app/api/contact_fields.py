from __future__ import annotations

from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Response, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.contacts import (
    ContactFieldDefinitionCreate,
    ContactFieldDefinitionResponse,
    ContactFieldDefinitionUpdate,
)
from app.security.permissions import TenantPrincipal, require_permission
from app.services.contact_fields import (
    ContactFieldConflictError,
    ContactFieldNotFoundError,
    ContactFieldService,
)

router = APIRouter(prefix="/contact-fields", tags=["contact-fields"])


def _service(session: Session, principal: TenantPrincipal) -> ContactFieldService:
    return ContactFieldService(session, principal.tenant_id, principal.user_id)


def _error(exc: Exception) -> NoReturn:
    if isinstance(exc, ContactFieldNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    if isinstance(exc, ContactFieldConflictError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    raise exc


@router.get("", response_model=list[ContactFieldDefinitionResponse])
def list_field_definitions(principal: TenantPrincipal = Depends(require_permission("contacts.read")), session: Session = Depends(get_db)) -> list[ContactFieldDefinitionResponse]:
    return [ContactFieldDefinitionResponse.model_validate(item) for item in _service(session, principal).list_fields()]


@router.post("", response_model=ContactFieldDefinitionResponse, status_code=status.HTTP_201_CREATED)
def create_field_definition(payload: ContactFieldDefinitionCreate, principal: TenantPrincipal = Depends(require_permission("contacts.create")), session: Session = Depends(get_db)) -> ContactFieldDefinitionResponse:
    try:
        return ContactFieldDefinitionResponse.model_validate(_service(session, principal).create(payload))
    except (ContactFieldConflictError, ContactFieldNotFoundError) as exc:
        _error(exc)


@router.get("/{field_id}", response_model=ContactFieldDefinitionResponse)
def get_field_definition(field_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.read")), session: Session = Depends(get_db)) -> ContactFieldDefinitionResponse:
    try:
        return ContactFieldDefinitionResponse.model_validate(_service(session, principal).get(field_id))
    except ContactFieldNotFoundError as exc:
        _error(exc)


@router.patch("/{field_id}", response_model=ContactFieldDefinitionResponse)
def update_field_definition(field_id: UUID, payload: ContactFieldDefinitionUpdate, principal: TenantPrincipal = Depends(require_permission("contacts.update")), session: Session = Depends(get_db)) -> ContactFieldDefinitionResponse:
    try:
        return ContactFieldDefinitionResponse.model_validate(_service(session, principal).update(field_id, payload))
    except (ContactFieldConflictError, ContactFieldNotFoundError) as exc:
        _error(exc)


@router.delete("/{field_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_field_definition(field_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.delete")), session: Session = Depends(get_db)) -> Response:
    try:
        _service(session, principal).delete(field_id)
    except ContactFieldNotFoundError as exc:
        _error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)