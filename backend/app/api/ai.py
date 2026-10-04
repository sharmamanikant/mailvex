from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import AIGeneration
from app.schemas.ai import (
    AIGenerationResponse,
    GenerateEmailRequest,
    GenerationTransformRequest,
)
from app.security.permissions import TenantPrincipal, require_permission
from app.services.ai import AIGenerationNotFoundError, AIService

router = APIRouter(prefix="/ai", tags=["ai-message-studio"])


def response(generation: AIGeneration) -> AIGenerationResponse:
    output = generation.generated_output
    return AIGenerationResponse(id=generation.id, workflow_status=generation.workflow_status, subject=str(output.get("subject", "")), body=str(output.get("body", "")), cta=str(output.get("cta", "")), personalization_suggestions=[str(item) for item in output.get("personalization_suggestions", [])], follow_up=output.get("follow_up", []), prompt_tokens=generation.prompt_tokens or 0, completion_tokens=generation.completion_tokens or 0, total_tokens=generation.total_tokens or 0, estimated_cost=float(generation.estimated_cost or 0), cost_currency=generation.cost_currency, created_at=generation.created_at)


@router.post("/generate-email", response_model=AIGenerationResponse, status_code=status.HTTP_201_CREATED)
def generate_email(payload: GenerateEmailRequest, principal: TenantPrincipal = Depends(require_permission("templates.create")), session: Session = Depends(get_db)) -> AIGenerationResponse:
    return response(AIService(session, principal.tenant_id, principal.user_id).generate_email(payload))


@router.get("/generations/{generation_id}", response_model=AIGenerationResponse)
def get_generation(generation_id: UUID, principal: TenantPrincipal = Depends(require_permission("templates.read")), session: Session = Depends(get_db)) -> AIGenerationResponse:
    try:
        return response(AIService(session, principal.tenant_id).get(generation_id))
    except AIGenerationNotFoundError:
        raise HTTPException(status_code=404, detail="AI generation not found") from None


@router.post("/generations/{generation_id}/transform", response_model=AIGenerationResponse, status_code=status.HTTP_201_CREATED)
def transform_generation(generation_id: UUID, payload: GenerationTransformRequest, principal: TenantPrincipal = Depends(require_permission("templates.create")), session: Session = Depends(get_db)) -> AIGenerationResponse:
    try:
        generation = AIService(session, principal.tenant_id, principal.user_id).transform(generation_id, payload.instruction, payload.tone, payload.language)
    except AIGenerationNotFoundError:
        raise HTTPException(status_code=404, detail="AI generation not found") from None
    return response(generation)
