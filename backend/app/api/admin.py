from __future__ import annotations

from uuid import UUID

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, EmailStr, Field
from sqlalchemy import delete, func, select, update
from sqlalchemy.orm import Session

from app.billing import EVENT_TEAM_MEMBER_ADDED, UsageService
from app.core.database import get_db
from app.models import (
    AuditLog,
    Permission,
    RefreshToken,
    Role,
    Team,
    TeamMember,
    User,
    UserRole,
)
from app.security.passwords import hash_password
from app.security.permissions import TenantPrincipal, require_permission
from app.services.audit import AuditService

router = APIRouter(prefix="/admin", tags=["administration"])
ROLE_NAMES = {"SUPER_ADMIN", "ADMIN", "TEAM_MANAGER", "CAMPAIGN_MANAGER", "SALES_RECRUITER", "VIEWER"}
ROLE_LABELS = {name: name.replace("_", " ").title() for name in ROLE_NAMES}


class UserCreate(BaseModel):
    email: EmailStr
    display_name: str = Field(min_length=1, max_length=200)
    password: str = Field(min_length=12, max_length=256)
    role: str = "VIEWER"


class UserUpdate(BaseModel):
    display_name: str | None = Field(default=None, min_length=1, max_length=200)
    status: str | None = None
    role: str | None = None


class PasswordUpdate(BaseModel):
    password: str = Field(min_length=12, max_length=256)


class TeamCreate(BaseModel):
    name: str = Field(min_length=1, max_length=150)


class TeamUpdate(BaseModel):
    name: str = Field(min_length=1, max_length=150)


class TeamMemberRequest(BaseModel):
    user_id: UUID


def _audit(session: Session, principal: TenantPrincipal, action: str, resource_type: str, resource_id: UUID | None) -> None:
    AuditService(session, principal.tenant_id, principal.user_id).record(action, resource_type, resource_id)


def _role(session: Session, tenant_id: UUID, name: str) -> Role:
    canonical_name = name.strip().upper().replace(" ", "_")
    if canonical_name not in ROLE_NAMES:
        raise HTTPException(status_code=422, detail="Unsupported role")
    role = session.scalar(select(Role).where(Role.tenant_id == tenant_id, Role.name.in_([canonical_name, ROLE_LABELS[canonical_name]])))
    if role is None:
        role = session.scalar(select(Role).where(Role.tenant_id.is_(None), Role.name.in_([canonical_name, ROLE_LABELS[canonical_name]])))
    if role is None:
        raise HTTPException(status_code=422, detail="Role is not configured")
    return role


def _user_role_name(session: Session, user: User) -> str | None:
    return session.scalar(
        select(Role.name)
        .join(UserRole, UserRole.role_id == Role.id)
        .where(UserRole.user_id == user.id, UserRole.tenant_id == user.tenant_id)
        .order_by(Role.name)
    )


def _require_super_admin(principal: TenantPrincipal, role: str | None) -> None:
    if role and role.strip().upper().replace(" ", "_") == "SUPER_ADMIN" and not any(item.strip().upper().replace(" ", "_") == "SUPER_ADMIN" for item in principal.roles):
        raise HTTPException(status_code=403, detail="Only Super Admin can assign Super Admin")


@router.post("/users", status_code=status.HTTP_201_CREATED)
def create_user(payload: UserCreate, principal: TenantPrincipal = Depends(require_permission("settings.manage")), session: Session = Depends(get_db)) -> dict[str, object]:
    _require_super_admin(principal, payload.role)
    email = str(payload.email).strip().lower()
    if session.scalar(select(User).where(User.tenant_id == principal.tenant_id, User.email == email)) is not None:
        raise HTTPException(status_code=409, detail="User already exists")
    UsageService(session, principal.tenant_id, principal.user_id).meter(
        EVENT_TEAM_MEMBER_ADDED,
        resource_type="user",
    )
    user = User(tenant_id=principal.tenant_id, email=email, password_hash=hash_password(payload.password), display_name=payload.display_name, status="ACTIVE")
    role = _role(session, principal.tenant_id, payload.role)
    session.add(user)
    session.flush()
    session.add(UserRole(tenant_id=principal.tenant_id, user_id=user.id, role_id=role.id))
    _audit(session, principal, "USER_CREATED", "user", user.id)
    session.commit()
    return {"id": user.id, "email": user.email, "display_name": user.display_name, "status": user.status, "role": role.name}


@router.get("/users/{user_id}")
def get_user(user_id: UUID, principal: TenantPrincipal = Depends(require_permission("settings.manage")), session: Session = Depends(get_db)) -> dict[str, object]:
    user = session.scalar(select(User).where(User.id == user_id, User.tenant_id == principal.tenant_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    return {"id": user.id, "email": user.email, "display_name": user.display_name, "status": user.status}


@router.patch("/users/{user_id}")
def update_user(user_id: UUID, payload: UserUpdate, principal: TenantPrincipal = Depends(require_permission("settings.manage")), session: Session = Depends(get_db)) -> dict[str, object]:
    _require_super_admin(principal, payload.role)
    user = session.scalar(select(User).where(User.id == user_id, User.tenant_id == principal.tenant_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    if user.id == principal.user_id and payload.status == "DISABLED":
        raise HTTPException(status_code=409, detail="You cannot disable your own account")
    if payload.display_name is not None:
        user.display_name = payload.display_name
    if payload.status is not None:
        if payload.status not in {"ACTIVE", "DISABLED"}:
            raise HTTPException(status_code=422, detail="Invalid user status")
        user.status = payload.status
        if payload.status == "DISABLED":
            session.execute(update(RefreshToken).where(RefreshToken.tenant_id == principal.tenant_id, RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)).values(revoked_at=func.now()))
    if payload.role is not None:
        if user.id == principal.user_id and payload.role.strip().upper().replace(" ", "_") not in {"ADMIN", "SUPER_ADMIN"}:
            raise HTTPException(status_code=409, detail="You cannot remove your own administrative access")
        session.execute(delete(UserRole).where(UserRole.tenant_id == principal.tenant_id, UserRole.user_id == user.id))
        session.add(UserRole(tenant_id=principal.tenant_id, user_id=user.id, role_id=_role(session, principal.tenant_id, payload.role).id))
    _audit(session, principal, "USER_DISABLED" if payload.status == "DISABLED" else "USER_ENABLED" if payload.status == "ACTIVE" else "ROLE_CHANGED" if payload.role else "USER_UPDATED", "user", user.id)
    session.commit()
    return {"id": user.id, "email": user.email, "display_name": user.display_name, "status": user.status}


@router.post("/users/{user_id}/password", status_code=status.HTTP_204_NO_CONTENT)
def update_user_password(
    user_id: UUID,
    payload: PasswordUpdate,
    principal: TenantPrincipal = Depends(require_permission("settings.manage")),
    session: Session = Depends(get_db),
) -> None:
    user = session.scalar(select(User).where(User.id == user_id, User.tenant_id == principal.tenant_id))
    if user is None or user.status == "DELETED":
        raise HTTPException(status_code=404, detail="User not found")
    user.password_hash = hash_password(payload.password)
    session.execute(update(RefreshToken).where(RefreshToken.tenant_id == principal.tenant_id, RefreshToken.user_id == user.id, RefreshToken.revoked_at.is_(None)).values(revoked_at=func.now()))
    _audit(session, principal, "USER_PASSWORD_CHANGED", "user", user.id)
    session.commit()


@router.delete("/users/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_user(user_id: UUID, principal: TenantPrincipal = Depends(require_permission("settings.manage")), session: Session = Depends(get_db)) -> None:
    if user_id == principal.user_id:
        raise HTTPException(status_code=409, detail="You cannot delete your own account")
    user = session.scalar(select(User).where(User.id == user_id, User.tenant_id == principal.tenant_id))
    if user is None:
        raise HTTPException(status_code=404, detail="User not found")
    user.status = "DELETED"
    _audit(session, principal, "USER_DELETED", "user", user.id)
    session.commit()


@router.post("/teams", status_code=status.HTTP_201_CREATED)
def create_team(payload: TeamCreate, principal: TenantPrincipal = Depends(require_permission("settings.manage")), session: Session = Depends(get_db)) -> dict[str, object]:
    if session.scalar(select(Team).where(Team.tenant_id == principal.tenant_id, Team.name == payload.name)) is not None:
        raise HTTPException(status_code=409, detail="Team already exists")
    team = Team(tenant_id=principal.tenant_id, name=payload.name)
    session.add(team)
    session.flush()
    _audit(session, principal, "TEAM_CREATED", "team", team.id)
    session.commit()
    return {"id": team.id, "name": team.name}


@router.get("/teams/{team_id}")
def get_team(team_id: UUID, principal: TenantPrincipal = Depends(require_permission("settings.manage")), session: Session = Depends(get_db)) -> dict[str, object]:
    team = session.scalar(select(Team).where(Team.id == team_id, Team.tenant_id == principal.tenant_id))
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    members = session.scalars(select(User).join(TeamMember, TeamMember.user_id == User.id).where(TeamMember.team_id == team.id, TeamMember.tenant_id == principal.tenant_id, User.tenant_id == principal.tenant_id)).all()
    return {"id": team.id, "name": team.name, "members": [{"id": user.id, "email": user.email} for user in members]}


@router.patch("/teams/{team_id}")
def update_team(team_id: UUID, payload: TeamUpdate, principal: TenantPrincipal = Depends(require_permission("settings.manage")), session: Session = Depends(get_db)) -> dict[str, object]:
    team = session.scalar(select(Team).where(Team.id == team_id, Team.tenant_id == principal.tenant_id))
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    team.name = payload.name
    _audit(session, principal, "TEAM_UPDATED", "team", team.id)
    session.commit()
    return {"id": team.id, "name": team.name}


@router.delete("/teams/{team_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_team(team_id: UUID, principal: TenantPrincipal = Depends(require_permission("settings.manage")), session: Session = Depends(get_db)) -> None:
    team = session.scalar(select(Team).where(Team.id == team_id, Team.tenant_id == principal.tenant_id))
    if team is None:
        raise HTTPException(status_code=404, detail="Team not found")
    session.delete(team)
    _audit(session, principal, "TEAM_DELETED", "team", team.id)
    session.commit()


@router.post("/teams/{team_id}/members", status_code=status.HTTP_201_CREATED)
def add_team_member(team_id: UUID, payload: TeamMemberRequest, principal: TenantPrincipal = Depends(require_permission("settings.manage")), session: Session = Depends(get_db)) -> dict[str, object]:
    team = session.scalar(select(Team).where(Team.id == team_id, Team.tenant_id == principal.tenant_id))
    user = session.scalar(select(User).where(User.id == payload.user_id, User.tenant_id == principal.tenant_id, User.status == "ACTIVE"))
    if team is None or user is None:
        raise HTTPException(status_code=404, detail="Team or user not found")
    if session.scalar(select(TeamMember).where(TeamMember.team_id == team.id, TeamMember.user_id == user.id, TeamMember.tenant_id == principal.tenant_id)) is not None:
        raise HTTPException(status_code=409, detail="User is already a team member")
    membership = TeamMember(tenant_id=principal.tenant_id, team_id=team.id, user_id=user.id)
    session.add(membership)
    _audit(session, principal, "TEAM_MEMBER_ADDED", "team", team.id)
    session.commit()
    return {"team_id": team.id, "user_id": user.id}


@router.delete("/teams/{team_id}/members/{user_id}", status_code=status.HTTP_204_NO_CONTENT)
def remove_team_member(team_id: UUID, user_id: UUID, principal: TenantPrincipal = Depends(require_permission("settings.manage")), session: Session = Depends(get_db)) -> None:
    membership = session.scalar(select(TeamMember).where(TeamMember.team_id == team_id, TeamMember.user_id == user_id, TeamMember.tenant_id == principal.tenant_id))
    if membership is None:
        raise HTTPException(status_code=404, detail="Team membership not found")
    session.delete(membership)
    _audit(session, principal, "TEAM_MEMBER_REMOVED", "team", team_id)
    session.commit()


@router.get("/users")
def list_users(
    principal: TenantPrincipal = Depends(require_permission("settings.manage")),
    session: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    status: str | None = None,
    email: str | None = None,
    name: str | None = None,
) -> dict[str, object]:
    query = select(User).where(User.tenant_id == principal.tenant_id)
    if status:
        query = query.where(User.status == status)
    if email:
        query = query.where(User.email.ilike(f"%{email.strip()}%"))
    if name:
        query = query.where(User.display_name.ilike(f"%{name.strip()}%"))
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    users = session.scalars(query.order_by(User.created_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [{"id": user.id, "email": user.email, "display_name": user.display_name, "status": user.status, "role": _user_role_name(session, user), "created_at": user.created_at} for user in users], "page": page, "page_size": page_size, "total": total}


@router.get("/teams")
def list_teams(
    principal: TenantPrincipal = Depends(require_permission("settings.manage")),
    session: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
) -> dict[str, object]:
    query = select(Team).where(Team.tenant_id == principal.tenant_id)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    teams = session.scalars(query.order_by(Team.created_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [{"id": team.id, "name": team.name, "created_at": team.created_at} for team in teams], "page": page, "page_size": page_size, "total": total}


@router.get("/roles")
def list_roles(principal: TenantPrincipal = Depends(require_permission("settings.manage")), session: Session = Depends(get_db)) -> list[dict[str, object]]:
    roles = session.scalars(select(Role).where((Role.tenant_id == principal.tenant_id) | Role.tenant_id.is_(None)).order_by(Role.name)).all()
    return [{"id": role.id, "name": role.name, "description": role.description, "is_system": role.is_system} for role in roles]


@router.get("/permissions")
def list_permissions(principal: TenantPrincipal = Depends(require_permission("settings.manage")), session: Session = Depends(get_db)) -> list[dict[str, object]]:
    permissions = session.scalars(select(Permission).order_by(Permission.key)).all()
    return [{"id": permission.id, "key": permission.key, "description": permission.description} for permission in permissions]


@router.get("/audit-logs")
def list_audit_logs(
    principal: TenantPrincipal = Depends(require_permission("audit.read")),
    session: Session = Depends(get_db),
    page: int = Query(1, ge=1),
    page_size: int = Query(50, ge=1, le=100),
    action: str | None = None,
    actor_user_id: UUID | None = None,
    entity_type: str | None = None,
) -> dict[str, object]:
    query = select(AuditLog).where(AuditLog.tenant_id == principal.tenant_id)
    if action:
        query = query.where(AuditLog.action == action)
    if actor_user_id:
        query = query.where(AuditLog.actor_id == actor_user_id)
    if entity_type:
        query = query.where(AuditLog.resource_type == entity_type)
    total = session.scalar(select(func.count()).select_from(query.subquery())) or 0
    logs = session.scalars(query.order_by(AuditLog.created_at.desc()).offset((page - 1) * page_size).limit(page_size)).all()
    return {"items": [{"id": item.id, "action": item.action, "entity_type": item.resource_type, "entity_id": item.resource_id, "actor_user_id": item.actor_id, "request_id": item.request_id, "timestamp": item.created_at, "metadata": item.audit_metadata} for item in logs], "page": page, "page_size": page_size, "total": total}
