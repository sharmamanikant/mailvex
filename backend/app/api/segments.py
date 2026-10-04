from __future__ import annotations

from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, Response, status
from sqlalchemy.orm import Session

from app.api.contacts import serialize_contact
from app.core.database import get_db
from app.schemas.contacts import ContactPage
from app.schemas.segments import (
    ContactSegmentCreate,
    ContactSegmentResponse,
    ContactSegmentUpdate,
)
from app.security.permissions import TenantPrincipal, require_permission
from app.services.segments import (
    SegmentConflictError,
    SegmentNotFoundError,
    SegmentService,
)

router = APIRouter(prefix="/segments", tags=["segments"])


def _service(session: Session, principal: TenantPrincipal) -> SegmentService:
    return SegmentService(session, principal.tenant_id, principal.user_id)


def _error(exc: Exception) -> NoReturn:
    if isinstance(exc, SegmentNotFoundError):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from None
    if isinstance(exc, SegmentConflictError):
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    raise exc


@router.get("", response_model=list[ContactSegmentResponse])
def list_segments(principal: TenantPrincipal = Depends(require_permission("contacts.read")), session: Session = Depends(get_db)) -> list[ContactSegmentResponse]:
    return [ContactSegmentResponse.model_validate(item) for item in _service(session, principal).list_segments()]


@router.post("", response_model=ContactSegmentResponse, status_code=status.HTTP_201_CREATED)
def create_segment(payload: ContactSegmentCreate, principal: TenantPrincipal = Depends(require_permission("contacts.create")), session: Session = Depends(get_db)) -> ContactSegmentResponse:
    try:
        return ContactSegmentResponse.model_validate(_service(session, principal).create(payload))
    except (SegmentConflictError, SegmentNotFoundError) as exc:
        _error(exc)


@router.get("/{segment_id}", response_model=ContactSegmentResponse)
def get_segment(segment_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.read")), session: Session = Depends(get_db)) -> ContactSegmentResponse:
    try:
        return ContactSegmentResponse.model_validate(_service(session, principal).get(segment_id))
    except SegmentNotFoundError as exc:
        _error(exc)


@router.patch("/{segment_id}", response_model=ContactSegmentResponse)
def update_segment(segment_id: UUID, payload: ContactSegmentUpdate, principal: TenantPrincipal = Depends(require_permission("contacts.update")), session: Session = Depends(get_db)) -> ContactSegmentResponse:
    try:
        return ContactSegmentResponse.model_validate(_service(session, principal).update(segment_id, payload))
    except (SegmentConflictError, SegmentNotFoundError) as exc:
        _error(exc)


@router.delete("/{segment_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_segment(segment_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.delete")), session: Session = Depends(get_db)) -> Response:
    try:
        _service(session, principal).delete(segment_id)
    except SegmentNotFoundError as exc:
        _error(exc)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get("/{segment_id}/contacts", response_model=ContactPage)
def segment_contacts(
    segment_id: UUID,
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    principal: TenantPrincipal = Depends(require_permission("contacts.read")),
    session: Session = Depends(get_db),
) -> ContactPage:
    try:
        result = _service(session, principal).evaluate(segment_id, page=page, page_size=page_size)
    except SegmentNotFoundError as exc:
        _error(exc)
    return ContactPage(items=[serialize_contact(item) for item in result.contacts], page=page, page_size=page_size, total=result.total)