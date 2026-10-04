from __future__ import annotations

import hashlib
from typing import Any
from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import SuppressionEntry, Unsubscribe
from app.security.permissions import TenantPrincipal, require_permission
from app.services.suppression import (
    SuppressionError,
    SuppressionService,
)
from app.services.suppression_engine import (
    SUPPRESSION_TYPES,
    SuppressionEngine,
    SuppressionEngineError,
)


class SuppressionEntryCreate(BaseModel):
    email: str = Field(min_length=3, max_length=320)
    type: str = Field(pattern="^(MANUAL|ADMIN_BLOCKED)$")
    reason: str | None = Field(default=None, max_length=500)

router = APIRouter(tags=["suppression"])


@router.get("/suppressions")
def list_suppressions(search: str | None = Query(None, max_length=200), reason: str | None = Query(None, max_length=30), principal: TenantPrincipal = Depends(require_permission("contacts.read")), session: Session = Depends(get_db)) -> list[dict[str, str]]:
    return [{"id": str(item.id), "email": item.email, "reason": item.reason, "source": item.source, "effective_at": item.effective_at.isoformat()} for item in SuppressionService(session, principal.tenant_id).list_suppressions(search, reason)]


@router.delete("/suppressions/{suppression_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_suppression(suppression_id: UUID, principal: TenantPrincipal = Depends(require_permission("contacts.update")), session: Session = Depends(get_db)) -> None:
    try:
        SuppressionService(session, principal.tenant_id).remove_suppression(suppression_id)
    except SuppressionError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Suppression not found") from None


@router.get("/unsubscribe/{token}")
def unsubscribe(token: str, session: Session = Depends(get_db)) -> dict[str, str]:
    token_hash = hashlib.sha256(token.encode()).hexdigest()
    record = session.scalar(select(Unsubscribe).where(Unsubscribe.token_hash == token_hash))
    if record is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid unsubscribe token")
    try:
        SuppressionService(session, record.tenant_id).consume_unsubscribe_token(token)
    except SuppressionError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Invalid unsubscribe token") from None
    return {"status": "unsubscribed", "message": "You have been unsubscribed."}


@router.post("/suppressions")
def create_suppression(email: str = Query(..., min_length=3, max_length=320), reason: str = Query(..., pattern="^(UNSUBSCRIBED|HARD_BOUNCE|COMPLAINT|INVALID|MANUAL_BLOCK|POLICY_BLOCK)$"), principal: TenantPrincipal = Depends(require_permission("contacts.update")), session: Session = Depends(get_db)) -> dict[str, str]:
    try:
        record = SuppressionService(session, principal.tenant_id).suppress(email, reason, "manual", None)
        session.commit()
    except SuppressionError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from None
    return {"id": str(record.id), "email": record.email, "reason": record.reason}


def _serialize_entry(entry: SuppressionEntry) -> dict[str, Any]:
    return {
        "id": str(entry.id),
        "email": entry.email_normalized,
        "type": entry.type,
        "source": entry.source,
        "reason": entry.reason,
        "provider": entry.provider,
        "campaign_id": str(entry.campaign_id) if entry.campaign_id else None,
        "protected": entry.type in {"COMPLAINT", "HARD_BOUNCE", "UNSUBSCRIBED"},
        "active": entry.active,
        "created_at": entry.created_at.isoformat() if entry.created_at else None,
        "updated_at": entry.updated_at.isoformat() if entry.updated_at else None,
    }


@router.get("/suppression-entries", tags=["suppression"])
def list_suppression_entries(
    type: str | None = Query(None, max_length=30),
    search: str | None = Query(None, max_length=200),
    include_inactive: bool = Query(False),
    principal: TenantPrincipal = Depends(require_permission("contacts.read")),
    session: Session = Depends(get_db),
) -> list[dict[str, Any]]:
    if type and type not in SUPPRESSION_TYPES:
        raise HTTPException(status_code=422, detail="Unknown suppression type")
    entries = SuppressionEngine(session, principal.tenant_id).list_entries(
        search=search, entry_type=type, include_inactive=include_inactive
    )
    return [_serialize_entry(entry) for entry in entries]


@router.post("/suppression-entries", status_code=status.HTTP_201_CREATED)
def create_suppression_entry(
    payload: SuppressionEntryCreate,
    principal: TenantPrincipal = Depends(require_permission("contacts.update")),
    session: Session = Depends(get_db),
) -> dict[str, Any]:
    engine = SuppressionEngine(session, principal.tenant_id, actor_id=principal.user_id)
    try:
        entry = engine.suppress(
            payload.email,
            payload.type,
            source="operator",
            reason=payload.reason or f"{payload.type} suppression",
        )
        session.commit()
    except SuppressionEngineError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from None
    return _serialize_entry(entry)


@router.delete("/suppression-entries/{entry_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_suppression_entry(
    entry_id: UUID,
    principal: TenantPrincipal = Depends(require_permission("contacts.update")),
    session: Session = Depends(get_db),
) -> None:
    engine = SuppressionEngine(session, principal.tenant_id, actor_id=principal.user_id)
    entry = engine.get(entry_id)
    if entry is None:
        raise HTTPException(status_code=404, detail="Suppression record not found")
    try:
        engine.remove(entry_id)
        session.commit()
    except SuppressionEngineError as exc:
        raise HTTPException(status_code=403, detail=str(exc)) from None


@router.get("/suppression-entries/types")
def suppression_entry_types() -> dict[str, Any]:
    return {"types": ["UNSUBSCRIBED", "HARD_BOUNCE", "COMPLAINT", "MANUAL", "ADMIN_BLOCKED"]}