from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Thread
from app.schemas.conversations import (
    ConversationDetailResponse,
    ConversationReplyDecision,
    ConversationReplyDraft,
    ConversationResponse,
    ConversationUpdate,
    ReplyResponse,
)
from app.security.permissions import TenantPrincipal, require_permission
from app.services.conversations import (
    ConversationError,
    ConversationNotFoundError,
    ConversationService,
)

router = APIRouter(prefix="/conversations", tags=["conversations"])


def conversation_response(thread: Thread) -> ConversationResponse:
    return ConversationResponse(id=thread.id, tenant_id=thread.tenant_id, provider_thread_id=thread.provider_thread_id, sender_id=thread.sender_id, contact_id=thread.contact_id, campaign_id=thread.campaign_id, subject=thread.subject, status=thread.status, assigned_user_id=thread.assigned_user_id, notes=thread.notes, tags=thread.tags, last_message_at=thread.last_message_at, reply_count=len(thread.replies))


def reply_response(reply) -> ReplyResponse:
    return ReplyResponse(id=reply.id, message_id=reply.message_id, sender_email=reply.sender_email, recipient_email=reply.recipient_email, body_text=reply.body_text, body_html=reply.body_html, classification=reply.classification, suggested_action=reply.suggested_action, suggested_response=reply.suggested_response, approval_status=reply.approval_status, received_at=reply.received_at)


def detail_response(thread: Thread) -> ConversationDetailResponse:
    return ConversationDetailResponse(conversation=conversation_response(thread), replies=[reply_response(reply) for reply in thread.replies])


def service(session: Session, principal: TenantPrincipal) -> ConversationService:
    return ConversationService(session, principal.tenant_id)


@router.get("", response_model=list[ConversationResponse])
def list_conversations(status: str | None = None, assigned_user_id: UUID | None = None, principal: TenantPrincipal = Depends(require_permission("campaigns.read")), session: Session = Depends(get_db)) -> list[ConversationResponse]:
    return [conversation_response(item) for item in service(session, principal).list(status, assigned_user_id)]


@router.get("/{thread_id}", response_model=ConversationDetailResponse)
def get_conversation(thread_id: UUID, principal: TenantPrincipal = Depends(require_permission("campaigns.read")), session: Session = Depends(get_db)) -> ConversationDetailResponse:
    try:
        return detail_response(service(session, principal).get(thread_id))
    except ConversationNotFoundError:
        raise HTTPException(status_code=404, detail="Conversation not found") from None


@router.patch("/{thread_id}", response_model=ConversationDetailResponse)
def update_conversation(thread_id: UUID, payload: ConversationUpdate, principal: TenantPrincipal = Depends(require_permission("campaigns.update")), session: Session = Depends(get_db)) -> ConversationDetailResponse:
    try:
        return detail_response(service(session, principal).update(thread_id, payload))
    except ConversationNotFoundError:
        raise HTTPException(status_code=404, detail="Conversation not found") from None
    except ConversationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/{thread_id}/replies/draft", response_model=ReplyResponse, status_code=201)
def create_reply_draft(thread_id: UUID, payload: ConversationReplyDraft, principal: TenantPrincipal = Depends(require_permission("campaigns.update")), session: Session = Depends(get_db)) -> ReplyResponse:
    try:
        return reply_response(service(session, principal).add_reply_draft(thread_id, payload.body_text))
    except ConversationNotFoundError:
        raise HTTPException(status_code=404, detail="Conversation not found") from None
    except ConversationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None


@router.post("/{thread_id}/replies/{reply_id}/decision", response_model=ReplyResponse)
def decide_reply(thread_id: UUID, reply_id: UUID, payload: ConversationReplyDecision, principal: TenantPrincipal = Depends(require_permission("campaigns.update")), session: Session = Depends(get_db)) -> ReplyResponse:
    try:
        return reply_response(service(session, principal).process_reply_decision(reply_id, payload))
    except ConversationNotFoundError:
        raise HTTPException(status_code=404, detail="Conversation reply not found") from None
    except ConversationError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
