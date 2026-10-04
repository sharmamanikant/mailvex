from __future__ import annotations

from typing import NoReturn
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import AIMessageDraft
from app.schemas.drafts import (
    AIMessageDraftListResponse,
    AIMessageDraftResponse,
    DraftApproveRequest,
    DraftGenerateRequest,
    DraftRejectRequest,
    DraftSaveTemplateRequest,
    DraftTransformRequest,
    DraftUpdate,
    DraftUsageResponse,
)
from app.security.permissions import TenantPrincipal, require_permission
from app.services.ai_drafts import (
    AIDraftCostLimitError,
    AIDraftError,
    AIDraftNotFoundError,
    AIDraftService,
)

router = APIRouter(prefix="/ai/drafts", tags=["ai-message-studio"])


def response(draft: AIMessageDraft) -> AIMessageDraftResponse:
    return AIMessageDraftResponse.model_validate(draft)


def service(session: Session, principal: TenantPrincipal) -> AIDraftService:
    return AIDraftService(session, principal.tenant_id, principal.user_id)


def handle_error(error: Exception) -> NoReturn:
    if isinstance(error, AIDraftNotFoundError):
        raise HTTPException(status_code=404, detail=str(error)) from None
    if isinstance(error, AIDraftCostLimitError):
        raise HTTPException(status_code=429, detail=str(error)) from None
    raise HTTPException(status_code=400, detail=str(error)) from None


@router.post("", response_model=AIMessageDraftResponse, status_code=status.HTTP_201_CREATED)
def create_draft(
    payload: DraftGenerateRequest,
    principal: TenantPrincipal = Depends(require_permission("templates.create")),
    session: Session = Depends(get_db),
) -> AIMessageDraftResponse:
    try:
        return response(service(session, principal).generate(payload))
    except (AIDraftError, AIDraftNotFoundError) as exc:
        handle_error(exc)


@router.get("", response_model=AIMessageDraftListResponse)
def list_drafts(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    principal: TenantPrincipal = Depends(require_permission("templates.read")),
    session: Session = Depends(get_db),
) -> AIMessageDraftListResponse:
    items, total = service(session, principal).list_drafts(page, page_size)
    return AIMessageDraftListResponse(items=[response(item) for item in items], total=total)


@router.get("/usage", response_model=DraftUsageResponse)
def draft_usage(
    principal: TenantPrincipal = Depends(require_permission("templates.read")),
    session: Session = Depends(get_db),
) -> DraftUsageResponse:
    return service(session, principal).usage()


@router.get("/{draft_id}", response_model=AIMessageDraftResponse)
def get_draft(
    draft_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("templates.read")),
    session: Session = Depends(get_db),
) -> AIMessageDraftResponse:
    try:
        return response(service(session, principal).get(draft_id))
    except AIDraftNotFoundError as exc:
        handle_error(exc)


@router.patch("/{draft_id}", response_model=AIMessageDraftResponse)
def update_draft(
    draft_id: UUID,
    payload: DraftUpdate,
    principal: TenantPrincipal = Depends(require_permission("templates.update")),
    session: Session = Depends(get_db),
) -> AIMessageDraftResponse:
    try:
        return response(service(session, principal).update(draft_id, payload))
    except (AIDraftError, AIDraftNotFoundError) as exc:
        handle_error(exc)


@router.post("/{draft_id}/transform", response_model=AIMessageDraftResponse)
def transform_draft(
    draft_id: UUID,
    payload: DraftTransformRequest,
    principal: TenantPrincipal = Depends(require_permission("templates.create")),
    session: Session = Depends(get_db),
) -> AIMessageDraftResponse:
    try:
        return response(service(session, principal).transform(draft_id, payload))
    except (AIDraftError, AIDraftNotFoundError) as exc:
        handle_error(exc)


@router.post("/{draft_id}/approve", response_model=AIMessageDraftResponse)
def approve_draft(
    draft_id: UUID,
    payload: DraftApproveRequest,
    principal: TenantPrincipal = Depends(require_permission("templates.create")),
    session: Session = Depends(get_db),
) -> AIMessageDraftResponse:
    try:
        return response(service(session, principal).approve(draft_id, payload))
    except (AIDraftError, AIDraftNotFoundError) as exc:
        handle_error(exc)


@router.post("/{draft_id}/reject", response_model=AIMessageDraftResponse)
def reject_draft(
    draft_id: UUID,
    payload: DraftRejectRequest,
    principal: TenantPrincipal = Depends(require_permission("templates.update")),
    session: Session = Depends(get_db),
) -> AIMessageDraftResponse:
    try:
        return response(service(session, principal).reject(draft_id, payload))
    except (AIDraftError, AIDraftNotFoundError) as exc:
        handle_error(exc)


@router.post("/{draft_id}/save-as-template", response_model=AIMessageDraftResponse)
def save_draft_as_template(
    draft_id: UUID,
    payload: DraftSaveTemplateRequest,
    principal: TenantPrincipal = Depends(require_permission("templates.create")),
    session: Session = Depends(get_db),
) -> AIMessageDraftResponse:
    try:
        return response(service(session, principal).save_as_template(draft_id, payload))
    except (AIDraftError, AIDraftNotFoundError) as exc:
        handle_error(exc)