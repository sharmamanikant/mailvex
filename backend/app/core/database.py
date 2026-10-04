from __future__ import annotations

from collections.abc import Generator
from datetime import UTC, datetime

from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session, sessionmaker

from app.core.config import settings
from app.models import (
    Base,
    ContactFieldDefinition,
    Permission,
    Role,
    RolePermission,
    Tenant,
    TenantSubscription,
    User,
    UserRole,
)
from app.security.passwords import hash_password

CONTACT_PERMISSIONS = (
    "contacts.read",
    "contacts.create",
    "contacts.update",
    "contacts.delete",
)

INTEGRATION_PERMISSIONS = (
    "integrations.read",
    "integrations.connect",
    "integrations.disconnect",
    "integrations.discover",
)

# Permissions granted to a newly provisioned workspace owner. Intentionally the
# same bundle used by the legacy signup flow so every owner workspace has one
# consistent feature set regardless of how it was created.
WORKSPACE_PERMISSION_KEYS = (
    "analytics.read",
    "billing.read",
    "billing.manage",
    *CONTACT_PERMISSIONS,
    *INTEGRATION_PERMISSIONS,
)

DEFAULT_CUSTOM_FIELDS = (
    ("skills", "Skills", "TEXT"),
    ("job_title", "Job Title", "TEXT"),
    ("experience", "Experience (years)", "NUMBER"),
    ("requirement", "Requirement", "TEXT"),
    ("service", "Service", "TEXT"),
    ("service_area", "Service Area", "TEXT"),
    ("budget", "Budget", "NUMBER"),
    ("technology", "Technology", "TEXT"),
    ("availability", "Availability", "SELECT"),
    ("candidate_type", "Candidate Type", "TEXT"),
    ("project_type", "Project Type", "TEXT"),
)

connect_args = {"check_same_thread": False} if settings.database_url.startswith("sqlite") else {}
engine = create_engine(settings.database_url, pool_pre_ping=True, connect_args=connect_args)
SessionLocal = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)


def initialize_database() -> None:
    if settings.app_env in ("production", "staging"):
        return
    Base.metadata.create_all(bind=engine)
    if not settings.database_url.startswith("sqlite"):
        return

    with SessionLocal() as session:
        tenant = session.scalar(select(Tenant).where(Tenant.slug == "development"))
        if tenant is None:
            tenant = Tenant(name="Development Tenant", slug="development", policy_defaults={})
            session.add(tenant)
            session.flush()

        role = session.scalar(select(Role).where(Role.tenant_id == tenant.id, Role.name == "Admin"))
        if role is None:
            role = Role(tenant_id=tenant.id, name="Admin", is_system=True, description="Admin")
            session.add(role)
            session.flush()

        subscription = session.scalar(select(TenantSubscription).where(TenantSubscription.tenant_id == tenant.id))
        if subscription is None:
            session.add(
                TenantSubscription(
                    tenant_id=tenant.id,
                    plan_code="free",
                    status="ACTIVE",
                    custom_limits={},
                    period_start=datetime.now(UTC),
                )
            )

        permission_keys = WORKSPACE_PERMISSION_KEYS
        permissions = {}
        for key in permission_keys:
            permission = session.scalar(select(Permission).where(Permission.key == key))
            if permission is None:
                permission = Permission(key=key, description="Development permission")
                session.add(permission)
                session.flush()
            permissions[key] = permission

        user = session.scalar(select(User).where(User.tenant_id == tenant.id, User.email == "owner@example.com"))
        if user is None:
            user = User(
                tenant_id=tenant.id,
                email="owner@example.com",
                password_hash=hash_password("correct horse battery staple"),
                display_name="Owner",
                status="ACTIVE",
            )
            session.add(user)
            session.flush()

        existing_assignment = session.scalar(
            select(UserRole).where(
                UserRole.tenant_id == tenant.id,
                UserRole.user_id == user.id,
                UserRole.role_id == role.id,
            )
        )
        if existing_assignment is None:
            session.add(UserRole(tenant_id=tenant.id, user_id=user.id, role_id=role.id))

        for permission in permissions.values():
            existing_permission = session.scalar(
                select(RolePermission).where(
                    RolePermission.role_id == role.id,
                    RolePermission.permission_id == permission.id,
                )
            )
            if existing_permission is None:
                session.add(RolePermission(role_id=role.id, permission_id=permission.id))

        for key, label, field_type in DEFAULT_CUSTOM_FIELDS:
            existing_field = session.scalar(
                select(ContactFieldDefinition).where(
                    ContactFieldDefinition.tenant_id == tenant.id,
                    ContactFieldDefinition.key == key,
                )
            )
            if existing_field is None:
                session.add(
                    ContactFieldDefinition(
                        tenant_id=tenant.id,
                        key=key,
                        label=label,
                        field_type=field_type,
                        options=["Immediate", "2 weeks", "1 month"] if key == "availability" else [],
                        required=False,
                    )
                )

        session.commit()


def get_db() -> Generator[Session, None, None]:
    session = SessionLocal()
    try:
        yield session
    finally:
        session.close()
