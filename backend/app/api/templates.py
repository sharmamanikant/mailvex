from __future__ import annotations

from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Template, TemplateVersion
from app.schemas.templates import (
    RenderedTemplate,
    TemplateCreate,
    TemplateDetail,
    TemplateListResponse,
    TemplatePreviewRequest,
    TemplateRecipientPreviewRequest,
    TemplateResponse,
    TemplateUpdate,
    TemplateVariablesResponse,
    TemplateVersionListResponse,
    TemplateVersionResponse,
)
from app.security.permissions import TenantPrincipal, require_permission
from app.services.templates import (
    RecipientNotFoundError,
    TemplateError,
    TemplateNotFoundError,
    TemplateService,
)

router = APIRouter(prefix="/templates", tags=["templates"])


def version_response(version: TemplateVersion) -> TemplateVersionResponse:
    return TemplateVersionResponse.model_validate(version)


def detail_response(template: Template) -> TemplateDetail:
    version = max(template.versions, key=lambda value: value.version_number)
    return TemplateDetail(template=TemplateResponse.model_validate(template), version=version_response(version))


def service(session: Session, principal: TenantPrincipal) -> TemplateService:
    return TemplateService(session, principal.tenant_id, principal.user_id)


def handle_error(error: Exception) -> NoReturn:
    if isinstance(error, (TemplateNotFoundError, RecipientNotFoundError)):
        raise HTTPException(status_code=404, detail=str(error)) from None
    raise HTTPException(status_code=400, detail=str(error)) from None


@router.get("/variables", response_model=TemplateVariablesResponse)
def list_variables(principal: TenantPrincipal = Depends(require_permission("templates.read")), session: Session = Depends(get_db)) -> TemplateVariablesResponse:
    registry = service(session, principal).variable_registry()
    return TemplateVariablesResponse(recipient=registry["recipient"], sender=registry["sender"])


@router.get("", response_model=TemplateListResponse)
def list_templates(principal: TenantPrincipal = Depends(require_permission("templates.read")), session: Session = Depends(get_db)) -> TemplateListResponse:
    items, total = service(session, principal).list_templates()
    return TemplateListResponse(items=[detail_response(item) for item in items], total=total)


@router.post("", response_model=TemplateDetail, status_code=status.HTTP_201_CREATED)
def create_template(payload: TemplateCreate, principal: TenantPrincipal = Depends(require_permission("templates.create")), session: Session = Depends(get_db)) -> TemplateDetail:
    try:
        return detail_response(service(session, principal).create(payload))
    except (TemplateError, TemplateNotFoundError) as exc:
        handle_error(exc)


@router.post("/{template_id}/preview", response_model=RenderedTemplate)
def preview_template(template_id: UUID, payload: TemplatePreviewRequest, principal: TenantPrincipal = Depends(require_permission("templates.read")), session: Session = Depends(get_db)) -> RenderedTemplate:
    try:
        return service(session, principal).render(template_id, payload)
    except (TemplateError, TemplateNotFoundError) as exc:
        handle_error(exc)


@router.post("/{template_id}/preview/contact/{contact_id}", response_model=RenderedTemplate)
def preview_template_for_contact(template_id: UUID, contact_id: UUID, payload: TemplateRecipientPreviewRequest, principal: TenantPrincipal = Depends(require_permission("templates.read")), session: Session = Depends(get_db)) -> RenderedTemplate:
    try:
        return service(session, principal).render_for_contact(template_id, contact_id, payload)
    except (TemplateError, TemplateNotFoundError, RecipientNotFoundError) as exc:
        handle_error(exc)


@router.get("/{template_id}/versions", response_model=TemplateVersionListResponse)
def list_template_versions(template_id: UUID, principal: TenantPrincipal = Depends(require_permission("templates.read")), session: Session = Depends(get_db)) -> TemplateVersionListResponse:
    try:
        items, total = service(session, principal).versions(template_id)
    except (TemplateError, TemplateNotFoundError) as exc:
        handle_error(exc)
    return TemplateVersionListResponse(items=[version_response(item) for item in items], total=total)


@router.get("/{template_id}/versions/{version_number}", response_model=TemplateVersionResponse)
def get_template_version(template_id: UUID, version_number: int, principal: TenantPrincipal = Depends(require_permission("templates.read")), session: Session = Depends(get_db)) -> TemplateVersionResponse:
    try:
        return version_response(service(session, principal).get_version(template_id, version_number))
    except (TemplateError, TemplateNotFoundError) as exc:
        handle_error(exc)


@router.patch("/{template_id}", response_model=TemplateDetail)
def update_template(template_id: UUID, payload: TemplateUpdate, principal: TenantPrincipal = Depends(require_permission("templates.update")), session: Session = Depends(get_db)) -> TemplateDetail:
    try:
        return detail_response(service(session, principal).update(template_id, payload))
    except (TemplateError, TemplateNotFoundError) as exc:
        handle_error(exc)


@router.post("/{template_id}/duplicate", response_model=TemplateDetail, status_code=status.HTTP_201_CREATED)
def duplicate_template(template_id: UUID, name: str = Query(..., min_length=1, max_length=150), principal: TenantPrincipal = Depends(require_permission("templates.create")), session: Session = Depends(get_db)) -> TemplateDetail:
    try:
        return detail_response(service(session, principal).duplicate(template_id, name))
    except (TemplateError, TemplateNotFoundError) as exc:
        handle_error(exc)


@router.post("/{template_id}/status", response_model=TemplateDetail)
def update_template_status(template_id: UUID, template_status: str = Query(..., alias="status", pattern="^(DRAFT|ACTIVE|ARCHIVED)$"), principal: TenantPrincipal = Depends(require_permission("templates.update")), session: Session = Depends(get_db)) -> TemplateDetail:
    try:
        return detail_response(service(session, principal).change_status(template_id, template_status))
    except (TemplateError, TemplateNotFoundError) as exc:
        handle_error(exc)


@router.get("/{template_id}", response_model=TemplateDetail)
def get_template(template_id: UUID, principal: TenantPrincipal = Depends(require_permission("templates.read")), session: Session = Depends(get_db)) -> TemplateDetail:
    try:
        return detail_response(service(session, principal).get(template_id))
    except (TemplateError, TemplateNotFoundError) as exc:
        handle_error(exc)