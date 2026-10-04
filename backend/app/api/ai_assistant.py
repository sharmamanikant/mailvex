from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from pydantic import BaseModel
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import AIReplyDraft
from app.security.permissions import TenantPrincipal, require_permission
from app.services.ai_assistant import (
    AIReplyAssistant,
    AIReplyAssistantError,
    AIReplyAssistantNotFoundError,
    AIReplyUnsubscribeError,
)

router = APIRouter(prefix="/inbox", tags=["ai-assistant"])


class DraftRequest(BaseModel):
    confirm_unsubscribe: bool = False


class TransformRequest(BaseModel):
    operation: str
    tone: str | None = None
    language: str | None = None


class RejectRequest(BaseModel):
    pass


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, AIReplyAssistantNotFoundError):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(exc, AIReplyUnsubscribeError):
        return HTTPException(status_code=409, detail=str(exc))
    if isinstance(exc, AIReplyAssistantError):
        return HTTPException(status_code=400, detail=str(exc))
    return HTTPException(status_code=500, detail="AI assistant service error")


def _draft_payload(draft: AIReplyDraft) -> dict[str, object]:
    return {
        "id": str(draft.id),
        "thread_id": str(draft.thread_id),
        "subject": draft.subject,
        "body": draft.body,
        "status": draft.status,
        "operation": draft.operation,
        "intent": draft.intent,
        "intent_confidence": draft.intent_confidence,
        "summary": draft.summary,
        "next_action": draft.next_action,
        "warnings": AIReplyAssistant.warnings_of(draft),
        "provider": draft.provider,
        "source_draft_id": str(draft.source_draft_id) if draft.source_draft_id else None,
    }


@router.post("/threads/{thread_id}/assistant/analyze")
def analyze_thread(
    thread_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    helper = AIReplyAssistant(session, principal.tenant_id, principal.user_id)
    try:
        return helper.analyze(thread_id)
    except Exception as exc:
        raise _error(exc) from None


@router.post("/threads/{thread_id}/assistant/draft")
def draft_reply(
    thread_id: UUID,
    payload: DraftRequest,
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    helper = AIReplyAssistant(session, principal.tenant_id, principal.user_id)
    try:
        draft = helper.draft(thread_id, confirm_unsubscribe=payload.confirm_unsubscribe)
    except Exception as exc:
        raise _error(exc) from None
    return _draft_payload(draft)


@router.post("/threads/{thread_id}/assistant/summarize")
def summarize_thread(
    thread_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    helper = AIReplyAssistant(session, principal.tenant_id, principal.user_id)
    try:
        record = helper.summarize(thread_id)
    except Exception as exc:
        raise _error(exc) from None
    return _draft_payload(record)


@router.post("/threads/{thread_id}/assistant/next-action")
def next_action(
    thread_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    helper = AIReplyAssistant(session, principal.tenant_id, principal.user_id)
    try:
        record = helper.next_action(thread_id)
    except Exception as exc:
        raise _error(exc) from None
    return _draft_payload(record)


@router.post("/drafts/{draft_id}/transform")
def transform_draft(
    draft_id: UUID,
    payload: TransformRequest,
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    helper = AIReplyAssistant(session, principal.tenant_id, principal.user_id)
    try:
        draft = helper.transform(
            draft_id,
            payload.operation,
            tone=payload.tone,
            language=payload.language,
        )
    except Exception as exc:
        raise _error(exc) from None
    return _draft_payload(draft)


@router.post("/drafts/{draft_id}/reject")
def reject_draft(
    draft_id: UUID,
    payload: RejectRequest,
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    helper = AIReplyAssistant(session, principal.tenant_id, principal.user_id)
    try:
        draft = helper.reject(draft_id)
    except Exception as exc:
        raise _error(exc) from None
    return _draft_payload(draft)
