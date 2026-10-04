"""Map verified external identities to application users and tenants (Part 3).

Resolution order:

1. ``external_identity_id`` — a previously linked identity returns its user.
2. Single user by normalized email — a controlled legacy link: the identity
   adopts an existing app account that already owns that email. Fails (with
   ``IdentityMappingError``) when more than one account matches, because we
   must never guess which tenant the user belongs to.
3. Provisioning — a brand new identity gets a brand new tenant with a system
   OWNER role. OWNER is the workspace's top application role and is never the
   global ADMIN role, so no identity ever auto-grants super-user rights.

Every step is audited (``CLERK_IDENTITY_LINKED``) and commits transactionally
with an IntegrityError-safe repair path for concurrent first logins.
"""

from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.core.database import WORKSPACE_PERMISSION_KEYS
from app.identity.schemas import VerifiedIdentity
from app.models import (
    Permission,
    Role,
    RolePermission,
    Tenant,
    TenantSubscription,
    User,
    UserRole,
)
from app.services.audit import AuditService


class IdentityMappingError(ValueError):
    """The identity cannot be mapped to a single application account."""


class IdentityMapper:
    def __init__(self, session: Session) -> None:
        self.session = session

    # ------------------------------------------------------------------ #
    # Public entry point
    # ------------------------------------------------------------------ #
    def resolve_app_user(self, identity: VerifiedIdentity) -> tuple[User, Tenant]:
        user = self.session.scalar(
            select(User).where(
                User.external_identity_id == identity.external_identity_id,
                User.identity_provider == identity.identity_provider,
            )
        )
        if user is not None:
            self._ensure_active(user)
            self._sync_profile(user, identity)
            self.session.commit()
            return user, user.tenant

        try:
            user, tenant = self._link_by_email(identity) or self._provision(identity)
        except IntegrityError:
            # A concurrent request provisioned the same identity first; adopt it.
            self.session.rollback()
            raced = self.session.scalar(
                select(User).where(
                    User.external_identity_id == identity.external_identity_id,
                    User.identity_provider == identity.identity_provider,
                )
            )
            if raced is None:
                raise
            user, tenant = raced, raced.tenant
        self.session.commit()
        return user, tenant

    # ------------------------------------------------------------------ #
    # Resolution paths
    # ------------------------------------------------------------------ #
    def _link_by_email(self, identity: VerifiedIdentity) -> tuple[User, Tenant] | None:
        email = identity.mapped_email
        if "@" not in email or email.endswith("@identity.local"):
            return None
        matches = list(
            self.session.scalars(select(User).where(func.lower(User.email) == email.lower())).all()
        )
        if len(matches) > 1:
            raise IdentityMappingError(
                "Multiple application accounts share this email; contact your administrator"
            )
        if len(matches) != 1:
            return None
        user = matches[0]
        self._ensure_active(user)
        user.external_identity_id = identity.external_identity_id
        user.identity_provider = identity.identity_provider
        self._sync_profile(user, identity)
        AuditService(self.session, user.tenant_id, user.id).record(
            "CLERK_IDENTITY_LINKED",
            "user",
            user.id,
            {
                "linked": True,
                "provisioned": False,
                "external_identity_id": identity.external_identity_id,
            },
        )
        return user, user.tenant

    def _provision(self, identity: VerifiedIdentity) -> tuple[User, Tenant]:
        email = identity.mapped_email
        display_name = identity.display_name or email.split("@")[0] or "New User"
        tenant = Tenant(name=f"{display_name}'s Workspace", slug=f"tenant-{uuid4().hex[:8]}")
        self.session.add(tenant)
        self.session.flush()
        self.session.add(
            TenantSubscription(
                tenant_id=tenant.id,
                plan_code="free",
                status="ACTIVE",
                custom_limits={},
                period_start=datetime.now(UTC),
            )
        )
        role = Role(tenant_id=tenant.id, name="OWNER", is_system=True, description="Workspace owner")
        self.session.add(role)
        self.session.flush()
        permissions = self._workspace_permissions()
        user = User(
            tenant_id=tenant.id,
            email=email,
            password_hash=None,
            display_name=display_name,
            status="ACTIVE",
            external_identity_id=identity.external_identity_id,
            identity_provider=identity.identity_provider,
            last_login_at=datetime.now(UTC),
        )
        self.session.add(user)
        self.session.flush()
        self.session.add(UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id))
        self.session.add_all(RolePermission(role_id=role.id, permission_id=permission.id) for permission in permissions)
        AuditService(self.session, tenant.id, user.id).record(
            "CLERK_IDENTITY_LINKED",
            "user",
            user.id,
            {
                "linked": False,
                "provisioned": True,
                "external_identity_id": identity.external_identity_id,
            },
        )
        return user, tenant

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def _workspace_permissions(self) -> list[Permission]:
        permissions: list[Permission] = []
        for key in WORKSPACE_PERMISSION_KEYS:
            permission = self.session.scalar(select(Permission).where(Permission.key == key))
            if permission is None:
                permission = Permission(key=key, description="Workspace permission")
                self.session.add(permission)
                self.session.flush()
            permissions.append(permission)
        return permissions

    def _ensure_active(self, user: User) -> None:
        if user.status != "ACTIVE":
            raise IdentityMappingError("Application account is disabled")

    def _sync_profile(self, user: User, identity: VerifiedIdentity) -> None:
        if identity.display_name and not user.display_name:
            user.display_name = identity.display_name
        if identity.email and user.email.endswith("@identity.local"):
            user.email = identity.email.lower()
        user.last_login_at = datetime.now(UTC)