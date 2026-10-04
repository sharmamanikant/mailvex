from __future__ import annotations

import logging
from collections.abc import Callable
from dataclasses import dataclass
from uuid import UUID

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.core.database import get_db
from app.identity.clerk import ClerkTokenError, get_verifier
from app.identity.mapping import IdentityMapper, IdentityMappingError
from app.identity.schemas import VerifiedIdentity
from app.models import Permission, Role, RolePermission, User, UserRole
from app.security.tokens import InvalidTokenError, TokenService

bearer_scheme = HTTPBearer(auto_error=False)
logger = logging.getLogger("crcrm.auth")


@dataclass(frozen=True)
class TenantPrincipal:
    user_id: UUID
    tenant_id: UUID
    email: str
    display_name: str
    roles: tuple[str, ...]


def _token_service() -> TokenService:
    return TokenService(settings)


def _load_role_names(session: Session, user: User) -> tuple[str, ...]:
    return tuple(
        session.scalars(
            select(Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id, UserRole.tenant_id == user.tenant_id)
        ).all()
    )


def _principal_from_identity(session: Session, identity: VerifiedIdentity) -> TenantPrincipal:
    user, _ = IdentityMapper(session).resolve_app_user(identity)
    return TenantPrincipal(user.id, user.tenant_id, user.email, user.display_name, _load_role_names(session, user))


def _principal_from_legacy_token(session: Session, token: str) -> TenantPrincipal:
    try:
        payload = _token_service().decode_access_token(token)
        user_id = UUID(str(payload["sub"]))
        tenant_id = UUID(str(payload["tenant_id"]))
    except (InvalidTokenError, ValueError, KeyError):
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token") from None
    user = session.scalar(select(User).where(User.id == user_id, User.tenant_id == tenant_id, User.status == "ACTIVE"))
    if user is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token")
    return TenantPrincipal(user.id, user.tenant_id, user.email, user.display_name, _load_role_names(session, user))


def get_current_principal(
    credentials: HTTPAuthorizationCredentials | None = Depends(bearer_scheme),
    session: Session = Depends(get_db),
) -> TenantPrincipal:
    """Resolve the bearer token to a tenant principal.

    Two paths, checked in order (Part 5 — controlled legacy migration):

    1. Clerk JWT verification (System A). Active only when ``clerk_issuer``
       is configured; on a failed verification we do NOT fall through to the
       legacy store — a broken/tampered Clerk token is a hard 401.
    2. Legacy HS256 token store while ``legacy_auth_enabled`` is true.

    When the legacy flag is switched off, legacy tokens stop working at the
    boundary without deleting any code or data.
    """
    if credentials is None:
        raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Authentication required")
    if session is None:
        raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Authentication service unavailable")
    token = credentials.credentials

    verifier = get_verifier()
    if verifier is not None:
        try:
            return _principal_from_identity(session, verifier.verify(token))
        except ClerkTokenError as exc:
            logger.warning("clerk_token_rejected reason=%s", str(exc))
            if not settings.legacy_auth_enabled:
                raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token") from None
        except IdentityMappingError as exc:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=str(exc)) from None

    if settings.legacy_auth_enabled:
        return _principal_from_legacy_token(session, token)
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid authentication token")


def require_permission(permission_key: str) -> Callable[..., TenantPrincipal]:
    def dependency(principal: TenantPrincipal = Depends(get_current_principal), session: Session = Depends(get_db)) -> TenantPrincipal:
        if session is None:
            raise HTTPException(status_code=status.HTTP_500_INTERNAL_SERVER_ERROR, detail="Authorization service unavailable")
        normalized_roles = {role.strip().upper().replace(" ", "_") for role in principal.roles}
        if normalized_roles & {"ADMIN", "SUPER_ADMIN", "OWNER"}:
            return principal
        allowed = session.scalar(
            select(Permission.id)
            .join(RolePermission, RolePermission.permission_id == Permission.id)
            .join(Role, Role.id == RolePermission.role_id)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(
                Permission.key == permission_key,
                UserRole.user_id == principal.user_id,
                UserRole.tenant_id == principal.tenant_id,
                (Role.tenant_id == principal.tenant_id) | (Role.tenant_id.is_(None)),
            )
        )
        if allowed is None:
            raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Insufficient permissions")
        return principal

    return dependency


def require_super_admin(principal: TenantPrincipal = Depends(get_current_principal)) -> TenantPrincipal:
    """System-level guard for global/cross-tenant operations.

    Ops observability queries platform-wide data (all tenants), so it must not
    be reachable by an ordinary tenant admin. Only users holding the
    SUPER_ADMIN role may access these endpoints.
    """
    if "SUPER_ADMIN" not in {role.strip().upper().replace(" ", "_") for role in principal.roles}:
        raise HTTPException(status_code=status.HTTP_403_FORBIDDEN, detail="Super admin privileges required")
    return principal
