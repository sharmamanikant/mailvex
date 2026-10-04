from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.core.database import get_db
from app.models import RefreshToken, Role, Tenant, User, UserRole
from app.security.passwords import hash_password
from app.security.permissions import TenantPrincipal, require_super_admin
from app.services.audit import AuditService

router = APIRouter(prefix="/platform-admin", tags=["platform administration"])


class PlatformUserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    status: str | None = None
    role: str | None = None


class PlatformPasswordUpdate(BaseModel):
    password: str = Field(min_length=12, max_length=256)


def _role_name(session: Session, user: User) -> str | None:
    return session.scalar(
        select(Role.name).join(UserRole, UserRole.role_id == Role.id).where(
            UserRole.user_id == user.id,
            UserRole.tenant_id == user.tenant_id,
        ).order_by(Role.name)
    )


def _audit(session: Session, principal: TenantPrincipal, action: str, resource_type: str, resource_id: UUID) -> None:
    AuditService(session, principal.tenant_id, principal.user_id).record(action, resource_type, resource_id)


@router.get("/overview")
def overview(
    principal: TenantPrincipal = Depends(require_super_admin),
    session: Session = Depends(get_db),
) -> dict[str, int]:
    return {
        "workspaces": int(session.scalar(select(func.count(Tenant.id))) or 0),
        "users": int(session.scalar(select(func.count(User.id)).where(User.status != "DELETED")) or 0),
        "active_users": int(session.scalar(select(func.count(User.id)).where(User.status == "ACTIVE")) or 0),
        "disabled_users": int(session.scalar(select(func.count(User.id)).where(User.status == "DISABLED")) or 0),
    }


@router.get("/workspaces")
def workspaces(
    principal: TenantPrincipal = Depends(require_super_admin),
    session: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
) -> dict[str, object]:
    query = select(Tenant).order_by(Tenant.created_at.desc())
    total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    items = session.scalars(query.offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [{"id": item.id, "name": item.name, "slug": item.slug, "status": item.status, "created_at": item.created_at} for item in items], "page": page, "page_size": page_size, "total": total}


@router.get("/users")
def users(
    principal: TenantPrincipal = Depends(require_super_admin),
    session: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(100, ge=1, le=200),
    tenant_id: UUID | None = None,
) -> dict[str, object]:
    query = select(User).where(User.status != "DELETED")
    if tenant_id:
        query = query.where(User.tenant_id == tenant_id)
    total = int(session.scalar(select(func.count()).select_from(query.subquery())) or 0)
    items = session.scalars(query.order_by(User.created_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [{"id": user.id, "tenant_id": user.tenant_id, "email": user.email, "display_name": user.display_name, "status": user.status, "role": _role_name(session, user), "created_at": user.created_at} for user in items], "page": page, "page_size": page_size, "total": total}


@router.patch("/users/{user_id}")
def update_user(
    user_id: UUID,
    payload: PlatformUserUpdate,
    principal: TenantPrincipal = Depends(require_super_admin),
    session: Session = Depends(get_db),
) -> dict[str, object]:
    user = session.get(User, user_id)
    if user is None or user.status == "DELETED":
        raise HTTPException(status_code=404, detail="User not found")
    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.status is not None:
        if payload.status not in {"ACTIVE", "DISABLED"}:
            raise HTTPException(status_code=422, detail="Invalid user status")
        user.status = payload.status
        if payload.status == "DISABLED":
            session.execute(update(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)).values(revoked_at=func.now()))
    if payload.role is not None:
        role_name = payload.role.strip().upper().replace(" ", "_")
        role = session.scalar(select(Role).where(Role.name.in_([role_name, role_name.replace("_", " ")]), (Role.tenant_id == user.tenant_id) | Role.tenant_id.is_(None)))
        if role is None:
            raise HTTPException(status_code=422, detail="Role is not configured")
        session.execute(delete(UserRole).where(UserRole.user_id == user.id, UserRole.tenant_id == user.tenant_id))
        session.add(UserRole(tenant_id=user.tenant_id, user_id=user.id, role_id=role.id))
    _audit(session, principal, "PLATFORM_USER_UPDATED", "user", user.id)
    session.commit()
    return {"id": user.id, "tenant_id": user.tenant_id, "email": user.email, "display_name": user.display_name, "status": user.status, "role": _role_name(session, user)}


@router.post("/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def update_password(
    user_id: UUID,
    payload: PlatformPasswordUpdate,
    principal: TenantPrincipal = Depends(require_super_admin),
    session: Session = Depends(get_db),
) -> None:
    user = session.get(User, user_id)
    if user is None or user.status == "DELETED":
        raise HTTPException(status_code=404, detail="User not found")
    user.password_hash = hash_password(payload.password)
    session.execute(update(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)).values(revoked_at=func.now()))
    _audit(session, principal, "PLATFORM_USER_PASSWORD_CHANGED", "user", user.id)
    session.commit()
