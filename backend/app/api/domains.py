from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import Domain
from app.security.permissions import TenantPrincipal, require_permission
from app.services.health import DomainHealthService

router = APIRouter(prefix="/domains", tags=["domains"])


class DomainCreate(BaseModel):
    domain: str = Field(..., min_length=1, max_length=253)


def _serialize(domain: Domain) -> dict[str, object]:
    return {
        "id": str(domain.id),
        "tenant_id": str(domain.tenant_id),
        "domain": domain.domain,
        "health_status": domain.health_status,
        "created_at": domain.created_at.isoformat() if domain.created_at else None,
        "updated_at": domain.updated_at.isoformat() if domain.updated_at else None,
    }


@router.get("")
def list_domains(principal: TenantPrincipal = Depends(require_permission("senders.read")), session: Session = Depends(get_db)) -> dict[str, object]:
    domains = session.scalars(
        select(Domain).where(Domain.tenant_id == principal.tenant_id).order_by(Domain.domain)
    ).all()
    return {"domains": [_serialize(domain) for domain in domains], "count": len(domains)}


@router.post("", status_code=status.HTTP_201_CREATED)
def create_domain(payload: DomainCreate, principal: TenantPrincipal = Depends(require_permission("senders.connect")), session: Session = Depends(get_db)) -> dict[str, object]:
    existing = session.scalar(
        select(Domain).where(Domain.tenant_id == principal.tenant_id, Domain.domain == payload.domain.strip().lower())
    )
    if existing is not None:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail="Domain already exists")
    domain = Domain(
        tenant_id=principal.tenant_id,
        domain=payload.domain.strip().lower(),
        health_status="UNKNOWN",
    )
    session.add(domain)
    session.commit()
    session.refresh(domain)
    return _serialize(domain)


@router.get("/{domain_id}")
def get_domain(domain_id: UUID, principal: TenantPrincipal = Depends(require_permission("senders.read")), session: Session = Depends(get_db)) -> dict[str, object]:
    domain = session.scalar(
        select(Domain).where(Domain.id == domain_id, Domain.tenant_id == principal.tenant_id)
    )
    if domain is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found")
    return _serialize(domain)


@router.get("/{domain_id}/health")
def get_domain_health(domain_id: UUID, principal: TenantPrincipal = Depends(require_permission("senders.read")), session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        return DomainHealthService(session, principal.tenant_id).evaluate(domain_id)
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found") from None


@router.post("/{domain_id}/health-check")
def check_domain_health(domain_id: UUID, principal: TenantPrincipal = Depends(require_permission("senders.connect")), session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        return DomainHealthService(session, principal.tenant_id).evaluate(domain_id)
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found") from None


@router.get("/{domain_id}/history")
def get_domain_history(domain_id: UUID, principal: TenantPrincipal = Depends(require_permission("senders.read")), session: Session = Depends(get_db)) -> dict[str, object]:
    try:
        return DomainHealthService(session, principal.tenant_id).history(domain_id)
    except LookupError:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Domain not found") from None
