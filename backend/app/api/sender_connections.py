"""Sender-connection alias router (Part 12 spec surface).

First-class CRUD + lifecycle routes under ``/api/v1/sender-connections`` that
delegate to ``IntegrationService`` (System B). Mirrors the singular
``/integrations`` routes so the Connect Wizard uses the spec-exact paths while
remaining a thin facade over one implementation.
"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.schemas.integrations import (
    DiscoveryResponse,
    IntegrationCreate,
    SenderConnectionResponse,
)
from app.security.permissions import TenantPrincipal, require_permission
from app.services.integrations import (
    IntegrationConflictError,
    IntegrationNotFoundError,
    IntegrationService,
    IntegrationValidationError,
    serialize_connection,
)

router = APIRouter(prefix="/sender-connections", tags=["sender-connections"])


def _service(session: Session, principal: TenantPrincipal) -> IntegrationService:
    return IntegrationService(session, principal.tenant_id)


def _response(data: dict[str, Any]) -> SenderConnectionResponse:
    return SenderConnectionResponse(**data)


@router.get("", response_model=list[SenderConnectionResponse])
def list_connections(
    principal: TenantPrincipal = Depends(require_permission("integrations.read")),
    session: Session = Depends(get_db),
) -> list[SenderConnectionResponse]:
    return [_response(serialize_connection(connection)) for connection in _service(session, principal).list_connections()]


@router.post("", response_model=SenderConnectionResponse, status_code=status.HTTP_201_CREATED)
def create_connection(
    payload: IntegrationCreate,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
) -> SenderConnectionResponse:
    try:
        connection = _service(session, principal).create_connection(payload, created_by=principal.user_id)
    except IntegrationValidationError as exc:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=str(exc)) from None
    except IntegrationConflictError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from None
    return _response(serialize_connection(connection))


@router.get("/{connection_id}", response_model=SenderConnectionResponse)
def get_connection(
    connection_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.read")),
    session: Session = Depends(get_db),
) -> SenderConnectionResponse:
    try:
        connection = _service(session, principal).get_connection(connection_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None
    return _response(serialize_connection(connection))


@router.post("/{connection_id}/validate", response_model=SenderConnectionResponse)
def validate_connection(
    connection_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.connect")),
    session: Session = Depends(get_db),
) -> SenderConnectionResponse:
    try:
        connection = _service(session, principal).validate_connection(connection_id, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None
    return _response(serialize_connection(connection))


@router.post("/{connection_id}/discover", response_model=DiscoveryResponse)
def discover_senders(
    connection_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.discover")),
    session: Session = Depends(get_db),
) -> DiscoveryResponse:
    try:
        return _service(session, principal).discover_senders(connection_id, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None


@router.post("/{connection_id}/disconnect", response_model=SenderConnectionResponse)
def disconnect_connection(
    connection_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("integrations.disconnect")),
    session: Session = Depends(get_db),
) -> SenderConnectionResponse:
    try:
        connection = _service(session, principal).disconnect_connection(connection_id, actor_id=principal.user_id)
    except IntegrationNotFoundError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Integration not found") from None
    return _response(serialize_connection(connection))