from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Campaign, Contact, EmailAccount, EmailMessage, EmailThread
from app.security.permissions import TenantPrincipal, require_permission
from app.services.inbox import (
    InboxError,
    InboxNotFoundError,
    InboxNotSupportedError,
    InboxService,
    InboxSyncError,
)
from app.services.inbox_reply import (
    InboxReplyBlockedError,
    InboxReplyError,
    InboxReplyNotFoundError,
    InboxReplyService,
)

router = APIRouter(prefix="/inbox", tags=["inbox"])


class ThreadStatusRequest(BaseModel):
    status: str = Field(min_length=1, max_length=30)


class SyncRequest(BaseModel):
    sender_id: UUID


class DraftReplyRequest(BaseModel):
    pass


class ApproveDraftRequest(BaseModel):
    draft_id: UUID


class ReplyRequest(BaseModel):
    subject: str = Field(min_length=1, max_length=998)
    body: str = Field(min_length=1, max_length=1_000_000)
    draft_id: UUID | None = None


def _error(exc: Exception) -> HTTPException:
    if isinstance(exc, (InboxNotFoundError, InboxReplyNotFoundError)):
        return HTTPException(status_code=404, detail=str(exc))
    if isinstance(
        exc,
        (InboxNotSupportedError, InboxReplyError, InboxReplyBlockedError, InboxError),
    ):
        status_code = 403 if isinstance(exc, InboxReplyBlockedError) else 400
        return HTTPException(status_code=status_code, detail=str(exc))
    if isinstance(exc, InboxSyncError):
        return HTTPException(status_code=502, detail=str(exc))
    return HTTPException(status_code=500, detail="Inbox service error")


def _enrich_thread(
    session: Session, thread: EmailThread, service: InboxService
) -> dict[str, object]:
    sender: EmailAccount | None = session.get(EmailAccount, thread.sender_id)
    contact: Contact | None = (
        session.get(Contact, thread.contact_id) if thread.contact_id else None
    )
    last = session.query(EmailMessage).filter(
        EmailMessage.thread_id == thread.id,
        EmailMessage.tenant_id == thread.tenant_id,
    ).order_by(EmailMessage.received_at.desc().nullslast()).first()
    return {
        "id": str(thread.id),
        "sender_id": str(thread.sender_id),
        "sender_email": sender.email if sender else None,
        "external_thread_id": thread.external_thread_id,
        "subject": thread.subject,
        "last_message_at": thread.last_message_at.isoformat()
        if thread.last_message_at
        else None,
        "status": thread.status,
        "match_status": thread.match_status,
        "provider": thread.provider,
        "campaign_id": str(thread.campaign_id) if thread.campaign_id else None,
        "contact_id": str(thread.contact_id) if thread.contact_id else None,
        "contact_name": (
            " ".join(
                p for p in (contact.first_name, contact.last_name) if p
            )
            or (contact.email if contact else None)
        )
        if contact
        else None,
        "snippet": (last.body_text or "")[:200] if last else None,
    }


def _thread_detail(
    session: Session, thread: EmailThread, service: InboxService
) -> dict[str, object]:
    messages = [
        {
            "id": str(m.id),
            "external_message_id": m.external_message_id,
            "direction": m.direction,
            "from_email": m.from_email,
            "to_email": m.to_email,
            "subject": m.subject,
            "body_text": m.body_text,
            "received_at": m.received_at.isoformat() if m.received_at else None,
            "status": m.status,
        }
        for m in thread.messages
    ]
    campaign: Campaign | None = (
        session.get(Campaign, thread.campaign_id) if thread.campaign_id else None
    )
    return {
        **_enrich_thread(session, thread, service),
        "messages": messages,
        "recipient": service.recipient_context(thread),
        "campaign_name": campaign.name if campaign else None,
    }


@router.get("/threads")
def list_threads(
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=200),
    status: str | None = None,
    sender_id: UUID | None = None,
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    service = InboxService(session, principal.tenant_id, principal.user_id)
    try:
        threads, total = service.list_threads(
            page=page, page_size=page_size, status=status, sender_id=sender_id
        )
    except Exception as exc:
        raise _error(exc) from None
    return {
        "items": [_enrich_thread(session, t, service) for t in threads],
        "total": total,
        "page": page,
        "page_size": page_size,
    }


@router.get("/threads/{thread_id}")
def get_thread(
    thread_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    service = InboxService(session, principal.tenant_id, principal.user_id)
    try:
        thread = service.get_thread(thread_id)
    except Exception as exc:
        raise _error(exc) from None
    return _thread_detail(session, thread, service)


@router.post("/threads/{thread_id}/status")
def set_thread_status(
    thread_id: UUID,
    payload: ThreadStatusRequest,
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    service = InboxService(session, principal.tenant_id, principal.user_id)
    try:
        thread = service.set_status(thread_id, payload.status)
    except Exception as exc:
        raise _error(exc) from None
    return _enrich_thread(session, thread, service)


@router.post("/sync")
def sync_sender(
    payload: SyncRequest,
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    service = InboxService(session, principal.tenant_id, principal.user_id)
    try:
        return service.sync_sender(payload.sender_id)
    except Exception as exc:
        raise _error(exc) from None


@router.post("/threads/{thread_id}/reply/draft")
def draft_reply(
    thread_id: UUID,
    payload: DraftReplyRequest,
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    service = InboxReplyService(session, principal.tenant_id, principal.user_id)
    try:
        draft = service.draft(thread_id)
    except Exception as exc:
        raise _error(exc) from None
    return {
        "draft_id": str(draft.id),
        "thread_id": str(draft.thread_id),
        "subject": draft.subject,
        "body": draft.body,
        "status": draft.status,
        "provider": draft.provider,
    }


@router.post("/threads/{thread_id}/reply/approve")
def approve_reply(
    thread_id: UUID,
    payload: ApproveDraftRequest,
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    service = InboxReplyService(session, principal.tenant_id, principal.user_id)
    try:
        draft = service.approve(payload.draft_id)
    except Exception as exc:
        raise _error(exc) from None
    return {
        "draft_id": str(draft.id),
        "thread_id": str(draft.thread_id),
        "status": draft.status,
    }


@router.post("/threads/{thread_id}/reply")
def send_reply(
    thread_id: UUID,
    payload: ReplyRequest,
    principal: TenantPrincipal = Depends(require_permission("analytics.read")),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    service = InboxReplyService(session, principal.tenant_id, principal.user_id)
    try:
        outgoing = service.send(
            thread_id, payload.subject, payload.body, draft_id=payload.draft_id
        )
    except Exception as exc:
        raise _error(exc) from None
    return {
        "message_id": str(outgoing.id),
        "thread_id": str(outgoing.thread_id),
        "direction": outgoing.direction,
        "status": outgoing.status,
    }
