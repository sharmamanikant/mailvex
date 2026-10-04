from __future__ import annotations

import secrets
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from uuid import UUID, uuid4

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session

from app.billing import EVENT_TEAM_MEMBER_ADDED, UsageService
from app.core.config import Settings
from app.core.database import WORKSPACE_PERMISSION_KEYS
from app.models import (
    PasswordResetToken,
    Permission,
    RefreshToken,
    Role,
    RolePermission,
    Tenant,
    TenantSubscription,
    User,
    UserRole,
)
from app.security.passwords import hash_password, verify_password
from app.security.tokens import TokenService, hash_refresh_token
from app.services.audit import AuditService


class AuthenticationError(ValueError):
    pass


@dataclass(frozen=True)
class IssuedTokens:
    access_token: str
    refresh_token: str
    access_expires_at: datetime
    refresh_expires_at: datetime


class AuthService:
    def __init__(self, session: Session, settings: Settings) -> None:
        self.session = session
        self.settings = settings
        self.tokens = TokenService(settings)

    def authenticate(self, email: str, password: str) -> User:
        user = self.session.scalar(select(User).where(User.email == email.strip().lower()))
        if user is None or user.password_hash is None or not verify_password(password, user.password_hash):
            raise AuthenticationError("Invalid email or password")
        if user.status != "ACTIVE":
            raise AuthenticationError("Invalid email or password")
        return user

    def signup(self, email: str, password: str, display_name: str) -> tuple[User, IssuedTokens]:
        normalized_email = email.strip().lower()
        if self.session.scalar(select(User).where(User.email == normalized_email)) is not None:
            raise AuthenticationError("Account already exists")

        # The first account created on a fresh install owns the platform and
        # receives SUPER_ADMIN. Without this the platform administration pages
        # are unreachable forever: require_super_admin only accepts SUPER_ADMIN,
        # and the user management API refuses to grant SUPER_ADMIN to anyone who
        # does not already hold it, so the first account could never promote
        # itself. No migration seeds the role either, so it is created here.
        is_first_user = self.session.scalar(select(func.count()).select_from(User)) == 0

        tenant = Tenant(name=f"{display_name}'s Workspace", slug=f"tenant-{uuid4().hex[:8]}")
        self.session.add(tenant)
        self.session.flush()

        # Every tenant gets a billing subscription from day one. The plan
        # catalog supplies the (data-driven) default limits; payment-provider
        # integration is deliberately absent.
        self.session.add(
            TenantSubscription(
                tenant_id=tenant.id,
                plan_code="free",
                status="ACTIVE",
                custom_limits={},
                period_start=datetime.now(UTC),
            )
        )

        role = Role(tenant_id=tenant.id, name="Admin", is_system=True, description="Workspace admin")
        self.session.add(role)
        self.session.flush()

        permissions = []
        for key in WORKSPACE_PERMISSION_KEYS:
            permission = self.session.scalar(select(Permission).where(Permission.key == key))
            if permission is None:
                permission = Permission(key=key, description="Workspace permission")
                self.session.add(permission)
                self.session.flush()
            permissions.append(permission)

        user = User(
            tenant_id=tenant.id,
            email=normalized_email,
            password_hash=hash_password(password),
            display_name=display_name,
            status="ACTIVE",
        )
        self.session.add(user)
        self.session.flush()

        self.session.add(UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id))
        self.session.add_all(RolePermission(role_id=role.id, permission_id=permission.id) for permission in permissions)

        if is_first_user:
            # Tenant-scoped like every other role here. Authorization keys off the
            # role name in the access token, and require_super_admin matches the
            # name only, so no role_permissions rows are required.
            platform_role = Role(
                tenant_id=tenant.id,
                name="SUPER_ADMIN",
                is_system=True,
                description="Platform administrator",
            )
            self.session.add(platform_role)
            self.session.flush()
            self.session.add(UserRole(tenant_id=tenant.id, user_id=user.id, role_id=platform_role.id))

        issued = self.issue_tokens(user)
        self._audit(user, "USER_CREATED", None)
        UsageService(self.session, tenant.id, user.id).meter(
            EVENT_TEAM_MEMBER_ADDED,
            resource_type="user",
            resource_id=user.id,
        )
        self.session.commit()
        return user, issued

    def _roles(self, user: User) -> list[str]:
        return list(self.session.scalars(
            select(Role.name)
            .join(UserRole, UserRole.role_id == Role.id)
            .where(UserRole.user_id == user.id, UserRole.tenant_id == user.tenant_id)
        ).all())

    def issue_tokens(
        self,
        user: User,
        family_id: UUID | None = None,
        replaced_token: RefreshToken | None = None,
    ) -> IssuedTokens:
        role_names = self._roles(user)
        access_token, access_expires_at = self.tokens.create_access_token(user.id, user.tenant_id, role_names)
        refresh_token, refresh_expires_at = self.tokens.create_refresh_token()
        token_id = uuid4()
        stored = RefreshToken(
            id=token_id,
            tenant_id=user.tenant_id,
            user_id=user.id,
            token_hash=hash_refresh_token(refresh_token),
            family_id=family_id or token_id,
            expires_at=refresh_expires_at,
        )
        self.session.add(stored)
        self.session.flush()
        if replaced_token is not None:
            replaced_token.replaced_by_id = stored.id
        return IssuedTokens(access_token, refresh_token, access_expires_at, refresh_expires_at)

    def rotate_refresh_token(self, raw_token: str, request_id: str | None = None) -> tuple[User, IssuedTokens]:
        now = datetime.now(UTC)
        stored = self.session.scalar(select(RefreshToken).where(
            RefreshToken.token_hash == hash_refresh_token(raw_token),
        ).with_for_update())
        if stored is None:
            raise AuthenticationError("Invalid refresh token")
        if stored.revoked_at is not None:
            self.session.execute(update(RefreshToken).where(
                RefreshToken.family_id == stored.family_id,
                RefreshToken.revoked_at.is_(None),
            ).values(revoked_at=now))
            self._audit(stored.user, "SECURITY_EVENT", request_id)
            self.session.commit()
            raise AuthenticationError("Refresh token reuse detected")
        expires_at = stored.expires_at
        if expires_at.tzinfo is None:
            expires_at = expires_at.replace(tzinfo=UTC)
        if expires_at <= now:
            raise AuthenticationError("Invalid refresh token")
        user = self.session.scalar(select(User).where(User.id == stored.user_id, User.tenant_id == stored.tenant_id, User.status == "ACTIVE"))
        if user is None:
            raise AuthenticationError("Invalid refresh token")
        stored.revoked_at = now
        issued = self.issue_tokens(user, family_id=stored.family_id, replaced_token=stored)
        self._audit(user, "REFRESH_TOKEN_ROTATED", request_id)
        self.session.commit()
        return user, issued

    def revoke_refresh_token(self, raw_token: str, request_id: str | None = None) -> None:
        stored = self.session.scalar(select(RefreshToken).where(RefreshToken.token_hash == hash_refresh_token(raw_token), RefreshToken.revoked_at.is_(None)))
        if stored is not None:
            stored.revoked_at = datetime.now(UTC)
            self._audit(stored.user, "LOGOUT", request_id)
            self.session.commit()

    def create_password_reset_token(self, email: str) -> str | None:
        user = self.session.scalar(select(User).where(User.email == email.strip().lower(), User.status == "ACTIVE"))
        if user is None:
            return None
        raw_token = secrets.token_urlsafe(48)
        self.session.add(PasswordResetToken(
            tenant_id=user.tenant_id,
            user_id=user.id,
            token_hash=hash_refresh_token(raw_token),
            expires_at=datetime.now(UTC) + timedelta(hours=1),
        ))
        self._audit(user, "PASSWORD_RESET_REQUEST", None)
        self.session.commit()
        return raw_token

    def remove_password_reset_token(self, raw_token: str) -> None:
        token = self.session.scalar(select(PasswordResetToken).where(PasswordResetToken.token_hash == hash_refresh_token(raw_token)))
        if token is not None:
            self.session.delete(token)
            self.session.commit()

    def reset_password(self, raw_token: str, new_password: str) -> None:
        now = datetime.now(UTC)
        token = self.session.scalar(select(PasswordResetToken).where(
            PasswordResetToken.token_hash == hash_refresh_token(raw_token),
            PasswordResetToken.used_at.is_(None),
            PasswordResetToken.expires_at > now,
        ))
        if token is None:
            raise AuthenticationError("Invalid or expired reset token")
        user = self.session.scalar(select(User).where(User.id == token.user_id, User.tenant_id == token.tenant_id))
        if user is None:
            raise AuthenticationError("Invalid or expired reset token")
        user.password_hash = hash_password(new_password)
        token.used_at = now
        self.session.execute(update(RefreshToken).where(RefreshToken.user_id == user.id, RefreshToken.tenant_id == user.tenant_id).values(revoked_at=now))
        self._audit(user, "PASSWORD_RESET_SUCCESS", None)
        self.session.commit()

    def _audit(self, user: User, action: str, request_id: str | None) -> None:
        AuditService(self.session, user.tenant_id, user.id, request_id).record(action, "user", user.id)
