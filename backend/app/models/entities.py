from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import (
    JSON,
    Boolean,
    DateTime,
    Float,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    func,
    text,
)
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column, relationship
from sqlalchemy.types import Uuid

from .base import Base, TenantOwnedMixin, UUIDTimestampMixin

JSONType = JSON().with_variant(JSONB(), "postgresql")


class Tenant(UUIDTimestampMixin, Base):
    __tablename__ = "tenants"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    slug: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True, index=True
    )
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    policy_defaults: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    users: Mapped[list[User]] = relationship(
        back_populates="tenant", cascade="all, delete-orphan"
    )


class User(TenantOwnedMixin, Base):
    __tablename__ = "users"
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    password_hash: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # System A identity: the external provider's stable user id. Null for
    # legacy accounts until they are linked to a Clerk identity. Unique across
    # tenants (a Clerk user owns exactly one application account).
    external_identity_id: Mapped[str | None] = mapped_column(
        String(100), unique=True
    )
    identity_provider: Mapped[str] = mapped_column(
        String(20), default="CLERK", nullable=False
    )
    tenant: Mapped[Tenant] = relationship(back_populates="users")
    team_memberships: Mapped[list[TeamMember]] = relationship(back_populates="user", overlaps="memberships,team")
    role_assignments: Mapped[list[UserRole]] = relationship(back_populates="user")
    refresh_tokens: Mapped[list[RefreshToken]] = relationship(back_populates="user", cascade="all, delete-orphan")
    password_reset_tokens: Mapped[list[PasswordResetToken]] = relationship(back_populates="user", cascade="all, delete-orphan")
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_users_tenant_email"),
        UniqueConstraint("tenant_id", "id", name="uq_users_tenant_id"),
    )


class Role(UUIDTimestampMixin, Base):
    __tablename__ = "roles"
    tenant_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), index=True
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    is_system: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    permissions: Mapped[list[RolePermission]] = relationship(
        back_populates="role", cascade="all, delete-orphan"
    )
    assignments: Mapped[list[UserRole]] = relationship(back_populates="role")
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_roles_tenant_name"),
    )


class Permission(UUIDTimestampMixin, Base):
    __tablename__ = "permissions"
    key: Mapped[str] = mapped_column(String(100), nullable=False, unique=True)
    description: Mapped[str | None] = mapped_column(String(500))
    roles: Mapped[list[RolePermission]] = relationship(back_populates="permission")


class RolePermission(UUIDTimestampMixin, Base):
    __tablename__ = "role_permissions"
    role_id: Mapped[UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    permission_id: Mapped[UUID] = mapped_column(
        ForeignKey("permissions.id", ondelete="CASCADE"), nullable=False
    )
    role: Mapped[Role] = relationship(back_populates="permissions")
    permission: Mapped[Permission] = relationship(back_populates="roles")
    __table_args__ = (
        UniqueConstraint("role_id", "permission_id", name="uq_role_permissions_pair"),
    )


class UserRole(UUIDTimestampMixin, Base):
    __tablename__ = "user_roles"
    tenant_id: Mapped[UUID] = mapped_column(
        ForeignKey("tenants.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    role_id: Mapped[UUID] = mapped_column(
        ForeignKey("roles.id", ondelete="CASCADE"), nullable=False
    )
    user: Mapped[User] = relationship(back_populates="role_assignments")
    role: Mapped[Role] = relationship(back_populates="assignments")
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "role_id", name="uq_user_roles_assignment"
        ),
    )


class RefreshToken(TenantOwnedMixin, Base):
    __tablename__ = "refresh_tokens"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    family_id: Mapped[UUID] = mapped_column(
        ForeignKey("refresh_tokens.id", ondelete="CASCADE"), nullable=False, index=True
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replaced_by_id: Mapped[UUID | None] = mapped_column(ForeignKey("refresh_tokens.id", ondelete="SET NULL"))
    user: Mapped[User] = relationship(back_populates="refresh_tokens", foreign_keys=[user_id])
    __table_args__ = (Index("ix_refresh_tokens_active", "tenant_id", "user_id", "expires_at"),)


class PasswordResetToken(TenantOwnedMixin, Base):
    __tablename__ = "password_reset_tokens"
    user_id: Mapped[UUID] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    user: Mapped[User] = relationship(back_populates="password_reset_tokens")
    __table_args__ = (Index("ix_password_reset_tokens_active", "tenant_id", "user_id", "expires_at"),)


class Team(TenantOwnedMixin, Base):
    __tablename__ = "teams"
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    memberships: Mapped[list[TeamMember]] = relationship(
        back_populates="team", cascade="all, delete-orphan", overlaps="team_memberships,user"
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_teams_tenant_name"),
        UniqueConstraint("tenant_id", "id", name="uq_teams_tenant_id"),
    )


class TeamMember(TenantOwnedMixin, Base):
    __tablename__ = "team_members"
    team_id: Mapped[UUID] = mapped_column(
        nullable=False
    )
    user_id: Mapped[UUID] = mapped_column(
        nullable=False
    )
    team: Mapped[Team] = relationship(back_populates="memberships", overlaps="team_memberships,user")
    user: Mapped[User] = relationship(back_populates="team_memberships", overlaps="memberships,team")
    __table_args__ = (
        UniqueConstraint("tenant_id", "team_id", "user_id", name="uq_team_membership"),
        ForeignKeyConstraint(["tenant_id", "team_id"], ["teams.tenant_id", "teams.id"], ondelete="CASCADE"),
        ForeignKeyConstraint(["tenant_id", "user_id"], ["users.tenant_id", "users.id"], ondelete="CASCADE"),
    )


class Contact(TenantOwnedMixin, Base):
    __tablename__ = "contacts"
    first_name: Mapped[str | None] = mapped_column(String(100))
    last_name: Mapped[str | None] = mapped_column(String(100))
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    phone: Mapped[str | None] = mapped_column(String(50))
    company: Mapped[str | None] = mapped_column(String(200))
    designation: Mapped[str | None] = mapped_column(String(200))
    location: Mapped[str | None] = mapped_column(String(200))
    website: Mapped[str | None] = mapped_column(String(500))
    industry: Mapped[str | None] = mapped_column(String(150))
    source: Mapped[str | None] = mapped_column(String(100))
    source_reference: Mapped[str | None] = mapped_column(String(500))
    notes: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    validation_status: Mapped[str] = mapped_column(
        String(30), default="UNKNOWN", nullable=False
    )
    suppression_status: Mapped[str] = mapped_column(
        String(30), default="CLEAR", nullable=False
    )
    # --- Recipient consent / lawful-basis metadata (guardrail 3) ----------
    # Importing a contact NEVER turns into "opted-in": new contacts default to
    # UNKNOWN. Only an explicit, evidenced action (recorded via the contacts
    # API with a source + timestamp + evidence) may set SUBSCRIBED.
    consent_status: Mapped[str] = mapped_column(
        String(30), default="UNKNOWN", nullable=False
    )  # UNKNOWN | SUBSCRIBED | UNSUBSCRIBED | SUPPRESSED
    consent_source: Mapped[str | None] = mapped_column(String(100))
    consent_timestamp: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    consent_evidence: Mapped[str | None] = mapped_column(String(500))
    unsubscribe_status: Mapped[str] = mapped_column(
        String(30), default="NONE", nullable=False
    )  # NONE | UNSUBSCRIBED
    unsubscribed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppressed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    suppression_reason: Mapped[str | None] = mapped_column(String(100))
    # --- Contact validation engine verdicts ---------------------------------
    # Deliberately separate from ``validation_status`` above, which is
    # campaign-exclusion semantics (services/campaigns.py and
    # services/compliance.py both drop INVALID / DISPOSABLE / ROLE_ACCOUNT
    # recipients). These columns are advisory technical signals written by the
    # validation worker; they must never gate sending on their own.
    email_status: Mapped[str] = mapped_column(
        String(30), default="UNKNOWN", nullable=False
    )  # VALID | INVALID | UNKNOWN
    email_type: Mapped[str] = mapped_column(
        String(30), default="UNKNOWN", nullable=False
    )  # FREE_MAILBOX | BUSINESS | ROLE | DISPOSABLE | UNKNOWN
    email_provider: Mapped[str] = mapped_column(
        String(60), default="UNKNOWN", nullable=False
    )  # GOOGLE | MICROSOFT | YAHOO | APPLE | PROTON | CUSTOM | UNKNOWN
    domain_status: Mapped[str] = mapped_column(
        String(30), default="UNKNOWN", nullable=False
    )  # EXISTS | NXDOMAIN | UNRESOLVED | UNKNOWN
    mx_status: Mapped[str] = mapped_column(
        String(30), default="UNKNOWN", nullable=False
    )  # VALID | MISSING | ERROR | UNKNOWN
    smtp_status: Mapped[str] = mapped_column(
        String(30), default="NOT_PROBED", nullable=False
    )  # ACCEPTED | REJECTED | TEMPORARY_FAILURE | TIMEOUT | CATCH_ALL | UNKNOWN | NOT_PROBED
    disposable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    role_account: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    catch_all: Mapped[bool | None] = mapped_column(Boolean, nullable=True)
    phone_status: Mapped[str] = mapped_column(
        String(30), default="UNKNOWN", nullable=False
    )  # VALID | INVALID | NOT_PROVIDED | UNKNOWN
    phone_type: Mapped[str] = mapped_column(
        String(30), default="UNKNOWN", nullable=False
    )  # MOBILE | LANDLINE | TOLL_FREE | VOIP | PREMIUM | UNKNOWN
    company_status: Mapped[str] = mapped_column(
        String(30), default="UNKNOWN", nullable=False
    )  # PRESENT | MISSING | DOMAIN_CONSISTENT | DOMAIN_INCONSISTENT
    duplicate_status: Mapped[str] = mapped_column(
        String(30), default="UNIQUE", nullable=False
    )  # UNIQUE | DUPLICATE | POSSIBLE_DUPLICATE
    duplicate_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    verification_score: Mapped[int | None] = mapped_column(Integer, nullable=True)
    risk_level: Mapped[str] = mapped_column(
        String(20), default="UNKNOWN", nullable=False
    )  # LOW | MEDIUM | HIGH | UNKNOWN
    verification_status: Mapped[str] = mapped_column(
        String(30), default="UNKNOWN", nullable=False
    )  # VERIFIED | LIKELY_VALID | NEEDS_REVIEW | RISKY | INVALID | DUPLICATE | UNKNOWN
    last_verified_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    verification_details: Mapped[dict[str, Any] | None] = mapped_column(JSONType, nullable=True)
    custom_fields: Mapped[list[ContactCustomField]] = relationship(
        back_populates="contact", cascade="all, delete-orphan"
    )
    list_memberships: Mapped[list[ContactListMember]] = relationship(
        back_populates="contact", cascade="all, delete-orphan"
    )
    tag_memberships: Mapped[list[ContactTagMember]] = relationship(
        back_populates="contact", cascade="all, delete-orphan"
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_contacts_tenant_email"),
        Index("ix_contacts_tenant_status", "tenant_id", "status"),
        Index("ix_contacts_tenant_company", "tenant_id", "company"),
        Index("ix_contacts_tenant_designation", "tenant_id", "designation"),
        Index("ix_contacts_tenant_location", "tenant_id", "location"),
        Index("ix_contacts_tenant_created", "tenant_id", "created_at"),
        Index("ix_contacts_tenant_verification_status", "tenant_id", "verification_status"),
        Index("ix_contacts_tenant_verification_score", "tenant_id", "verification_score"),
        Index("ix_contacts_tenant_risk_level", "tenant_id", "risk_level"),
        Index("ix_contacts_tenant_last_verified", "tenant_id", "last_verified_at"),
    )


class ContactCustomField(TenantOwnedMixin, Base):
    __tablename__ = "contact_custom_fields"
    contact_id: Mapped[UUID] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    field_key: Mapped[str] = mapped_column(String(100), nullable=False)
    field_value: Mapped[str | None] = mapped_column(Text)
    field_type: Mapped[str] = mapped_column(String(30), default="TEXT", nullable=False)
    contact: Mapped[Contact] = relationship(back_populates="custom_fields")
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "contact_id", "field_key", name="uq_contact_custom_field"
        ),
    )


class VerificationJob(TenantOwnedMixin, Base):
    """Bulk verification run. Counters are denormalised on purpose so the CRM UI
    can poll progress without ever holding contact rows in memory."""

    __tablename__ = "verification_jobs"
    created_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", nullable=False)
    scope: Mapped[str] = mapped_column(String(20), default="BULK", nullable=False)
    total_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    valid_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    invalid_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    risky_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    needs_review_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    unknown_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    request_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    celery_task_id: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_verification_jobs_tenant_status", "tenant_id", "status"),
        Index("ix_verification_jobs_tenant_created", "tenant_id", "created_at"),
    )


class ValidationDomainRule(UUIDTimestampMixin, Base):
    """Configurable reference data for the validation engine.

    Deliberately **global** rather than tenant-owned: these are universal,
    publicly-known facts about public DNS. Per-tenant copies would be a
    correctness hazard, and tenant isolation is enforced on all *results*
    (``contacts`` / ``verification_jobs``) instead.
    """

    __tablename__ = "validation_domain_rules"
    domain: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    provider: Mapped[str | None] = mapped_column(String(60))
    source: Mapped[str] = mapped_column(String(60), default="seed", nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "domain", "category", name="uq_validation_domain_rules_domain_category"
        ),
        Index("ix_validation_domain_rules_domain", "domain"),
    )


class ValidationLocalRule(UUIDTimestampMixin, Base):
    """Configurable local-part rules (role accounts such as info@ / sales@).

    Global for the same reason as ``ValidationDomainRule``: these are public
    conventions, not tenant data.
    """

    __tablename__ = "validation_local_rules"
    local_part: Mapped[str] = mapped_column(String(255), nullable=False)
    category: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(60), default="seed", nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "local_part", "category", name="uq_validation_local_rules_local_part_category"
        ),
        Index("ix_validation_local_rules_local_part", "local_part"),
    )


class ContactFieldDefinition(TenantOwnedMixin, Base):
    __tablename__ = "contact_field_definitions"
    key: Mapped[str] = mapped_column(String(100), nullable=False)
    label: Mapped[str] = mapped_column(String(150), nullable=False)
    field_type: Mapped[str] = mapped_column(String(30), nullable=False)
    options: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "key", name="uq_contact_field_definition_key"),)


class ContactSegment(TenantOwnedMixin, Base):
    __tablename__ = "contact_segments"
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    filters: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    __table_args__ = (UniqueConstraint("tenant_id", "name", name="uq_contact_segments_tenant_name"),)


class ContactList(TenantOwnedMixin, Base):
    __tablename__ = "contact_lists"
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    members: Mapped[list[ContactListMember]] = relationship(
        back_populates="contact_list", cascade="all, delete-orphan"
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_contact_lists_tenant_name"),
    )


class ContactListMember(TenantOwnedMixin, Base):
    __tablename__ = "contact_list_members"
    contact_list_id: Mapped[UUID] = mapped_column(
        ForeignKey("contact_lists.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[UUID] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    contact_list: Mapped[ContactList] = relationship(back_populates="members")
    contact: Mapped[Contact] = relationship(back_populates="list_memberships")
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "contact_list_id", "contact_id", name="uq_contact_list_member"
        ),
    )


class ContactTag(TenantOwnedMixin, Base):
    __tablename__ = "contact_tags"
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_contact_tags_tenant_name"),
    )


class ContactTagMember(TenantOwnedMixin, Base):
    __tablename__ = "contact_tag_members"
    contact_tag_id: Mapped[UUID] = mapped_column(
        ForeignKey("contact_tags.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[UUID] = mapped_column(
        ForeignKey("contacts.id", ondelete="CASCADE"), nullable=False
    )
    contact: Mapped[Contact] = relationship(back_populates="tag_memberships")
    tag: Mapped[ContactTag] = relationship()
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "contact_tag_id", "contact_id", name="uq_contact_tag_member"
        ),
    )


class ContactSource(TenantOwnedMixin, Base):
    __tablename__ = "contact_sources"
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    source_type: Mapped[str] = mapped_column(String(50), nullable=False)
    reference: Mapped[str | None] = mapped_column(String(500))
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_contact_sources_tenant_name"),
    )


class CompliancePolicy(UUIDTimestampMixin, Base):
    """A versioned platform policy document (Terms of Service / Acceptable Use).

    Policies are platform-level and versioned. Tenants/users record their
    acceptance against a specific ``(policy_type, policy_version)`` in
    :class:`PolicyAcceptance`; a materially updated policy publishes a new
    version and requires re-acceptance. Bodies are surfaced only through the
    policy endpoints and never treated as advice.
    """

    __tablename__ = "compliance_policies"
    policy_type: Mapped[str] = mapped_column(String(40), nullable=False)  # TERMS_OF_SERVICE | ACCEPTABLE_USE
    policy_version: Mapped[str] = mapped_column(String(20), nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str] = mapped_column(String(2000), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "policy_type", "policy_version", name="uq_compliance_policies_type_version"
        ),
        Index("ix_compliance_policies_type_version", "policy_type", "policy_version"),
    )


class PolicyAcceptance(TenantOwnedMixin, Base):
    """A tenant user's recorded acceptance of a specific policy version.

    Requirements: ``tenant_id``, ``user_id``, ``policy_version``,
    ``accepted_at``, ``policy_type``, ``ip_address``. Acceptance is asked once
    per user per policy; the current published version is re-offered whenever
    the policy is materially updated. Sending never re-prompts per email.
    """

    __tablename__ = "policy_acceptances"
    user_id: Mapped[UUID] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    policy_type: Mapped[str] = mapped_column(String(40), nullable=False)
    policy_version: Mapped[str] = mapped_column(String(20), nullable=False)
    accepted_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    ip_address: Mapped[str | None] = mapped_column(String(64))
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "user_id", "policy_type", name="uq_policy_acceptance_user_type"
        ),
        Index("ix_policy_acceptances_tenant_user", "tenant_id", "user_id"),
    )


class ComplianceProfile(TenantOwnedMixin, Base):
    """Tenant-level configurable compliance settings (guardrail 18).

    Jurisdiction-specific legal rules are intentionally NOT hardcoded into the
    sending engine. This configuration drives the send-time gates and the
    campaign pre-flight; changing a legal requirement is a configuration/data
    change, not a sending-engine rewrite. ``safety_thresholds`` powers the
    automatic protective-pausing machinery (guardrail 12); the defaults are
    explicitly labelled configurable, never presented as universally safe
    legal limits.
    """

    __tablename__ = "compliance_profiles"
    compliance_profile: Mapped[str] = mapped_column(
        String(50), default="STANDARD", nullable=False
    )
    jurisdiction: Mapped[str] = mapped_column(
        String(100), default="UNSPECIFIED", nullable=False
    )
    require_unsubscribe: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    require_sender_identity: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    require_policy_acceptance: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    require_consent_metadata: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    require_list_unsubscribe_header: Mapped[bool] = mapped_column(
        Boolean, default=True, nullable=False
    )
    retention_policy: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    safety_thresholds: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_compliance_profile_tenant"),
    )


class ImportJob(TenantOwnedMixin, Base):
    __tablename__ = "import_jobs"
    created_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    filename: Mapped[str] = mapped_column(String(255), nullable=False)
    file_type: Mapped[str] = mapped_column(String(20), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="UPLOADED", nullable=False)
    column_mapping: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    duplicate_policy: Mapped[str] = mapped_column(
        String(20), default="SKIP", nullable=False
    )
    counts: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    preview: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    total_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    processed_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    successful_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    failed_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    duplicate_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    suppressed_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    updated_rows: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    error_message: Mapped[str | None] = mapped_column(String(1000))
    error_report_ref: Mapped[str | None] = mapped_column(String(500))
    source_file_ref: Mapped[str | None] = mapped_column(String(500))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailAccount(TenantOwnedMixin, Base):
    __tablename__ = "email_accounts"
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    reply_to: Mapped[str | None] = mapped_column(String(320))
    status: Mapped[str] = mapped_column(String(30), default="CONNECTED", nullable=False)
    health_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    timezone: Mapped[str] = mapped_column(String(100), default="UTC", nullable=False)
    smtp_host: Mapped[str | None] = mapped_column(String(255))
    smtp_port: Mapped[int | None] = mapped_column(Integer)
    smtp_tls_mode: Mapped[str | None] = mapped_column(String(20))
    smtp_username: Mapped[str | None] = mapped_column(String(320))

    # --- Phase 10: Sender Account Management -------------------------------
    # Provider enum is stored in `provider` (GOOGLE / MICROSOFT / SMTP).
    # connection_status tracks the live connection state; `status` mirrors the
    # durable account status used by health/compliance.
    connection_status: Mapped[str] = mapped_column(String(30), default="CONNECTED", nullable=False)
    oauth_provider_account_id: Mapped[str | None] = mapped_column(String(500))
    # Tokens are ALWAYS stored Fernet-encrypted; never plaintext and never
    # surfaced through API responses.
    access_token_encrypted: Mapped[str | None] = mapped_column(Text)
    refresh_token_encrypted: Mapped[str | None] = mapped_column(Text)
    token_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    scopes: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Concurrency guard: set while an access-token refresh is running so that
    # multiple refreshes for the same sender never run in parallel.
    refresh_in_progress: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))

    # Relationships
    credential: Mapped[ProviderCredential | None] = relationship(
        back_populates="account", uselist=False, cascade="all, delete-orphan"
    )
    profile: Mapped[SenderProfile | None] = relationship(
        back_populates="account", uselist=False, cascade="all, delete-orphan"
    )
    health_history: Mapped[list[SenderHealthHistory]] = relationship(
        back_populates="sender", cascade="all, delete-orphan"
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_email_accounts_tenant_email"),
        Index("ix_email_accounts_tenant_status", "tenant_id", "status"),
    )


class SenderHealthHistory(TenantOwnedMixin, Base):
    __tablename__ = "sender_health_history"
    sender_id: Mapped[UUID] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), nullable=False)
    health_score: Mapped[Decimal] = mapped_column(Numeric(5, 2), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    factors: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    sender: Mapped[EmailAccount] = relationship(back_populates="health_history")
    __table_args__ = (Index("ix_sender_health_history_tenant_sender", "tenant_id", "sender_id"),)


class ProviderCredential(TenantOwnedMixin, Base):
    __tablename__ = "provider_credentials"
    email_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    encrypted_credential_ref: Mapped[str] = mapped_column(String(500), nullable=False)
    encrypted_credential_payload: Mapped[str | None] = mapped_column(Text)
    encryption_key_version: Mapped[str] = mapped_column(String(50), nullable=False)
    expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    account: Mapped[EmailAccount] = relationship(back_populates="credential")


class SenderProfile(TenantOwnedMixin, Base):
    __tablename__ = "sender_profiles"
    email_account_id: Mapped[UUID] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="CASCADE"), nullable=False, unique=True
    )
    company: Mapped[str | None] = mapped_column(String(200))
    designation: Mapped[str | None] = mapped_column(String(200))
    phone: Mapped[str | None] = mapped_column(String(50))
    signature: Mapped[str | None] = mapped_column(Text)
    account: Mapped[EmailAccount] = relationship(back_populates="profile")


class SenderConnection(TenantOwnedMixin, Base):
    """A provider integration (System B foundation).

    One connection owns many sender accounts and a *reference* to securely
    stored, encrypted credentials. Plaintext provider secrets are never
    persisted here and never leave the credential store.
    """

    __tablename__ = "sender_connections"
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    connection_type: Mapped[str] = mapped_column(String(20), nullable=False)  # OAUTH | API_KEY | SMTP
    status: Mapped[str] = mapped_column(String(30), default="CONNECTING", nullable=False)  # CONNECTING/CONNECTED/REAUTH_REQUIRED/DISCONNECTED/FAILED/DISABLED
    external_account_id: Mapped[str | None] = mapped_column(String(500))
    email: Mapped[str | None] = mapped_column(String(320))
    # Encrypted pointer to the secure credential store; never a raw secret.
    credential_reference: Mapped[str | None] = mapped_column(Text)
    credential_version: Mapped[str] = mapped_column(String(50), default="v1", nullable=False)
    credential_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connection_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSONType, default=dict, nullable=False)
    last_connected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    sender_accounts: Mapped[list[SenderAccount]] = relationship(
        back_populates="connection", cascade="all, delete-orphan"
    )
    __table_args__ = (
        Index("ix_sender_connections_tenant_status", "tenant_id", "status"),
        Index(
            "uq_sender_connections_google_account",
            "tenant_id",
            "provider",
            "external_account_id",
            unique=True,
            sqlite_where=text("provider = 'GOOGLE' AND external_account_id IS NOT NULL"),
            postgresql_where=text("provider = 'GOOGLE' AND external_account_id IS NOT NULL"),
        ),
        Index(
            "uq_sender_connections_microsoft_account",
            "tenant_id",
            "provider",
            "external_account_id",
            unique=True,
            sqlite_where=text("provider = 'MICROSOFT' AND external_account_id IS NOT NULL"),
            postgresql_where=text("provider = 'MICROSOFT' AND external_account_id IS NOT NULL"),
        ),
    )


class ProviderConnection(TenantOwnedMixin, Base):
    """An organization-level provider authorization (Phase 1 provider foundation).

    Distinct from :class:`SenderConnection` (an individual sender mailbox):
    a ``ProviderConnection`` represents the tenant's authorization to a
    provider workspace (e.g. a Google Workspace). Phase 2 will discover
    mailboxes against a CONNECTED provider connection and those mailboxes
    become the ``Sender`` records used for sending.

    Credentials are never stored in plaintext: only an opaque, encrypted
    ``credential_reference`` (Fernet, via the shared credential store) is
    persisted. The reference is invalidated (tombstoned) on disconnect.
    """

    __tablename__ = "provider_connections"
    provider: Mapped[str] = mapped_column(String(30), nullable=False)  # GOOGLE | MICROSOFT | SENDGRID | ZOHO | SMTP
    connection_type: Mapped[str] = mapped_column(String(20), nullable=False)  # OAUTH | API_KEY | SMTP
    status: Mapped[str] = mapped_column(
        String(30), default="CONNECTING", nullable=False
    )  # CONNECTING | CONNECTED | ERROR | DISCONNECTED | REVOKED
    provider_account_id: Mapped[str | None] = mapped_column(String(500))
    workspace_domain: Mapped[str | None] = mapped_column(String(255))
    display_name: Mapped[str | None] = mapped_column(String(200))
    scopes: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    # Encrypted pointer to the secure credential store; never a raw secret.
    credential_reference: Mapped[str | None] = mapped_column(Text)
    credential_version: Mapped[str] = mapped_column(String(50), default="v1", nullable=False)
    credential_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    connected_by: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    last_sync_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Sync metadata (Phase 2 - workspace mailbox discovery)
    last_sync_status: Mapped[str | None] = mapped_column(String(30))  # IDLE | SYNCING | COMPLETED | FAILED
    last_sync_error: Mapped[str | None] = mapped_column(Text)
    last_sync_started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_sync_completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Provider-authored, non-secret connection metadata (e.g. Microsoft 365
    # organization name / default domain / tenant id). Raw tokens never live
    # here -- only the encrypted ``credential_reference``.
    connection_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, default=dict, nullable=False
    )
    # Summary counts from the most recent mailbox sync run.
    last_sync_stats: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    mailboxes: Mapped[list[Mailbox]] = relationship(
        "Mailbox", back_populates="provider_connection", cascade="all, delete-orphan"
    )
    senders: Mapped[list[Sender]] = relationship(
        "Sender", back_populates="provider_connection", cascade="all, delete-orphan"
    )
    __table_args__ = (
        Index("ix_provider_connections_tenant_status", "tenant_id", "status"),
        Index("ix_provider_connections_tenant_provider", "tenant_id", "provider"),
        # Prevent duplicate provider accounts within one tenant without
        # blocking different tenants from connecting their own Workspace.
        Index(
            "uq_provider_connections_tenant_provider_account",
            "tenant_id",
            "provider",
            "provider_account_id",
            unique=True,
            sqlite_where=text("provider_account_id IS NOT NULL"),
            postgresql_where=text("provider_account_id IS NOT NULL"),
        ),
    )


class Mailbox(TenantOwnedMixin, Base):
    """A workspace user/mailbox discovered by provider sync (Phase 2).

    Mailboxes are the eligibility pool from which Phase 3 Sender records
    are created. A mailbox may exist without being a Sender.

    Only actual send-capable Workspace users (user_type ``USER``) are
    stored. Non-user directory objects are counted but not persisted.
    Credentials are never stored here; only the parent
    ``ProviderConnection`` holds encrypted credentials.
    """

    __tablename__ = "mailboxes"

    provider_connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_connections.id", ondelete="CASCADE"), nullable=False
    )
    provider_mailbox_id: Mapped[str] = mapped_column(String(500), nullable=False)
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    first_name: Mapped[str | None] = mapped_column(String(200))
    last_name: Mapped[str | None] = mapped_column(String(200))
    department: Mapped[str | None] = mapped_column(String(200))
    job_title: Mapped[str | None] = mapped_column(String(255))
    user_type: Mapped[str] = mapped_column(String(30), default="USER", nullable=False)
    provider_status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    is_suspended: Mapped[bool] = mapped_column(default=False, nullable=False)
    is_deleted: Mapped[bool] = mapped_column(default=False, nullable=False)
    last_discovered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    provider_connection: Mapped[ProviderConnection] = relationship(
        "ProviderConnection", back_populates="mailboxes"
    )
    senders: Mapped[list[Sender]] = relationship(
        "Sender", back_populates="mailbox", cascade="all, delete-orphan"
    )

    __table_args__ = (
        Index(
            "uq_mailboxes_tenant_connection_mailbox",
            "tenant_id",
            "provider_connection_id",
            "provider_mailbox_id",
            unique=True,
        ),
        Index("ix_mailboxes_tenant_connection", "tenant_id", "provider_connection_id"),
        Index("ix_mailboxes_tenant_email", "tenant_id", "email"),
    )


class Sender(TenantOwnedMixin, Base):
    """An application-level sending identity created from a discovered mailbox.

    A Sender always resolves to exactly one ``Mailbox`` and one
    ``ProviderConnection``. Senders are created only for eligible (ACTIVE, not
    suspended, not deleted) mailboxes, and a mailbox maps to at most one Sender
    per tenant (unique ``(tenant_id, mailbox_id)``).

    ``status`` and ``sending_enabled`` are intentionally separate: a sender can
    be ACTIVE (a valid identity) while ``sending_enabled`` is False and no
    traffic is routed to it. ``health_*`` fields are populated by the Phase 5
    health engine; they start UNKNOWN/null.
    """

    __tablename__ = "senders"

    mailbox_id: Mapped[UUID] = mapped_column(
        ForeignKey("mailboxes.id", ondelete="CASCADE"), nullable=False
    )
    provider_connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("provider_connections.id", ondelete="CASCADE"), nullable=False
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(30), nullable=False)

    # ACTIVE | DISABLED | ERROR | REVOKED | REMOVED
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)
    sending_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)

    # UNKNOWN | HEALTHY | WARNING | CRITICAL | CHECKING (populated in Phase 5)
    health_status: Mapped[str] = mapped_column(String(30), default="UNKNOWN", nullable=False)
    health_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    last_health_check_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    mailbox: Mapped[Mailbox] = relationship("Mailbox", back_populates="senders")
    provider_connection: Mapped[ProviderConnection] = relationship(
        "ProviderConnection", back_populates="senders"
    )
    health_checks: Mapped[list[SenderHealthCheck]] = relationship(
        "SenderHealthCheck", back_populates="sender", cascade="all, delete-orphan"
    )

    __table_args__ = (
        UniqueConstraint("tenant_id", "mailbox_id", name="uq_senders_tenant_mailbox"),
        Index("ix_senders_tenant_status", "tenant_id", "status"),
        Index("ix_senders_tenant_connection", "tenant_id", "provider_connection_id"),
        Index("ix_senders_tenant_email", "tenant_id", "email"),
        Index("ix_senders_tenant_provider", "tenant_id", "provider"),
    )


class SenderHealthCheck(TenantOwnedMixin, Base):
    """A single sender health evaluation run (Phase 5).

    Captures the overall outcome of running the provider-independent health
    checks for one ``Sender`` together with the versioned score used. The
    per-check outcomes live in :class:`SenderHealthCheckResult`.

    ``triggered_by`` records who started the evaluation (MANUAL | SCHEDULED |
    SYSTEM). ``error_code``/``error_message`` are populated only when the whole
    evaluation fails catastrophically (individual check failures are captured
    as WARNING/FAIL outcomes instead). No credentials, tokens, or raw DNS
    payloads are ever stored here.
    """

    __tablename__ = "sender_health_checks"

    sender_id: Mapped[UUID] = mapped_column(
        ForeignKey("senders.id", ondelete="CASCADE"), nullable=False
    )
    # UNKNOWN | HEALTHY | WARNING | CRITICAL | CHECKING
    overall_status: Mapped[str] = mapped_column(
        String(30), default="CHECKING", nullable=False
    )
    overall_score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    score_version: Mapped[str] = mapped_column(String(20), default="v1", nullable=False)
    # MANUAL | SCHEDULED | SYSTEM
    triggered_by: Mapped[str] = mapped_column(
        String(20), default="MANUAL", nullable=False
    )
    started_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_code: Mapped[str | None] = mapped_column(String(50))
    error_message: Mapped[str | None] = mapped_column(Text)

    sender: Mapped[Sender] = relationship("Sender", back_populates="health_checks")
    results: Mapped[list[SenderHealthCheckResult]] = relationship(
        "SenderHealthCheckResult",
        back_populates="health_check",
        cascade="all, delete-orphan",
    )

    __table_args__ = (
        Index(
            "ix_sender_health_checks_tenant_sender_created",
            "tenant_id",
            "sender_id",
            "created_at",
        ),
        Index("ix_sender_health_checks_sender_created", "sender_id", "created_at"),
    )


class SenderHealthCheckResult(TenantOwnedMixin, Base):
    """A single per-check outcome inside a :class:`SenderHealthCheck`.

    ``status`` is one of PASS | WARNING | FAIL | UNKNOWN | NOT_APPLICABLE.
    ``score`` is the check's own 0-100 score (UNKNOWN/NOT_APPLICABLE checks
    carry None) before it is multiplied by its documented weight.
    ``metadata`` never contains raw credentials, tokens, or secret DNS data.
    """

    __tablename__ = "sender_health_check_results"

    health_check_id: Mapped[UUID] = mapped_column(
        ForeignKey("sender_health_checks.id", ondelete="CASCADE"), nullable=False
    )
    # PROVIDER_CONNECTION | MAILBOX_STATUS | DOMAIN | SPF | DKIM | DMARC | DNS |
    # SENDING_CONFIGURATION | SENDING_SIGNALS
    check_type: Mapped[str] = mapped_column(String(30), nullable=False)
    # PASS | WARNING | FAIL | UNKNOWN | NOT_APPLICABLE
    status: Mapped[str] = mapped_column(String(20), default="UNKNOWN", nullable=False)
    score: Mapped[Decimal | None] = mapped_column(Numeric(5, 2))
    # INFO | LOW | MEDIUM | HIGH | CRITICAL
    severity: Mapped[str] = mapped_column(String(20), default="INFO", nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    summary: Mapped[str | None] = mapped_column(Text)
    technical_details: Mapped[str | None] = mapped_column(Text)
    recommendation: Mapped[str | None] = mapped_column(Text)
    result_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, default=dict, nullable=False
    )
    checked_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=lambda: datetime.now(UTC), nullable=False
    )

    health_check: Mapped[SenderHealthCheck] = relationship(
        "SenderHealthCheck", back_populates="results"
    )

    __table_args__ = (
        Index(
            "ix_sender_health_check_results_check",
            "health_check_id",
            "tenant_id",
        ),
        Index(
            "ix_sender_health_check_results_type_status",
            "tenant_id",
            "check_type",
            "status",
        ),
    )


class SenderAccount(TenantOwnedMixin, Base):
    """A single sender mailbox discovered/registered on a connection."""

    __tablename__ = "sender_accounts"
    connection_id: Mapped[UUID] = mapped_column(
        ForeignKey("sender_connections.id", ondelete="CASCADE"), nullable=False, index=True
    )
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    display_name: Mapped[str | None] = mapped_column(String(200))
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    external_sender_id: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(30), default="ACTIVE", nullable=False)  # ACTIVE/DISABLED/INVALID/PENDING_VERIFICATION
    health_status: Mapped[str] = mapped_column(String(30), default="UNKNOWN", nullable=False)
    # Traffic enablement + per-sender daily ceilings (Phase 10Q sender engine).
    # Flags never auto-enable: campaign/warmup traffic starts disabled until
    # the tenant explicitly opts in.
    campaign_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    warmup_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    reply_sync_enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    daily_campaign_limit: Mapped[int] = mapped_column(Integer, default=30, nullable=False)
    daily_warmup_limit: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    # Operational telemetry (provider-facing, never secrets).
    last_success_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_failure_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error: Mapped[str | None] = mapped_column(Text)
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # --- Protective-pausing state (guardrails 12, 23) ----------------------
    # Set by SenderSafetyService when configured risk thresholds trip. While
    # REVIEW_REQUIRED, new campaign sends are paused until a human review
    # explicitly re-enables the sender. Never auto-resumes after a serious
    # provider/compliance failure.
    compliance_status: Mapped[str] = mapped_column(
        String(30), default="COMPLIANT", nullable=False
    )  # COMPLIANT | WARNING | BLOCKED | REVIEW_REQUIRED
    compliance_reasons: Mapped[list[str]] = mapped_column(
        JSONType, default=list, nullable=False
    )
    compliance_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    paused_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    resume_guard: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    connection: Mapped[SenderConnection] = relationship(back_populates="sender_accounts")
    __table_args__ = (
        UniqueConstraint("tenant_id", "connection_id", "email", name="uq_sender_accounts_tenant_connection_email"),
        Index("ix_sender_accounts_tenant_connection", "tenant_id", "connection_id"),
        Index("ix_sender_accounts_tenant_status", "tenant_id", "status"),
    )


class SenderProviderMetadata(TenantOwnedMixin, Base):
    """Provider-specific per-sender metadata (Phase 10Q).

    Keeps provider-owned details (organization ids, SMTP/IMAP discovery,
    warmup provider fingerprints) out of the senders table. Each sender may
    have one metadata row per provider. Values are configuration, never
    secrets — credentials stay in the encrypted credential store.
    """

    __tablename__ = "sender_provider_metadata"
    sender_id: Mapped[UUID] = mapped_column(
        ForeignKey("sender_accounts.id", ondelete="CASCADE"), nullable=False
    )
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    metadata_json: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, default=dict, nullable=False
    )
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "sender_id",
            "provider",
            name="uq_sender_provider_metadata_tenant_sender_provider",
        ),
        Index("ix_sender_provider_metadata_tenant_provider", "tenant_id", "provider"),
    )


class WarmupSettings(TenantOwnedMixin, Base):
    """Per-sender mailbox warmup configuration (Phase 10Q).

    Warmup traffic runs on a separate queue from campaign traffic and only
    when ``sender_accounts.warmup_enabled`` is true. Delays are generated
    inside :data:`WarmupSettings.minimum_delay`/``maximum_delay`` for
    controlled, provider-compliant pacing — never to evade abuse controls.
    """

    __tablename__ = "warmup_settings"
    sender_id: Mapped[UUID] = mapped_column(
        ForeignKey("sender_accounts.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    daily_limit: Mapped[int] = mapped_column(Integer, default=20, nullable=False)
    reply_rate_target: Mapped[float] = mapped_column(Float, default=0.5, nullable=False)
    start_time: Mapped[str | None] = mapped_column(String(5))  # "HH:MM" 24h local
    end_time: Mapped[str | None] = mapped_column(String(5))
    weekdays: Mapped[list[str]] = mapped_column(
        JSONType, default=lambda: ["MON", "TUE", "WED", "THU", "FRI"], nullable=False  # e.g. ["MON", "TUE", ...]
    )
    minimum_delay: Mapped[int] = mapped_column(Integer, default=60, nullable=False)  # seconds
    maximum_delay: Mapped[int] = mapped_column(Integer, default=3600, nullable=False)  # seconds
    target_provider_distribution: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "sender_id", name="uq_warmup_settings_tenant_sender"),
    )


class Domain(TenantOwnedMixin, Base):
    __tablename__ = "domains"
    domain: Mapped[str] = mapped_column(String(253), nullable=False)
    health_status: Mapped[str] = mapped_column(
        String(20), default="UNKNOWN", nullable=False
    )
    checks: Mapped[list[DomainCheck]] = relationship(
        back_populates="domain_record", cascade="all, delete-orphan"
    )
    history: Mapped[list[DomainHealthHistory]] = relationship(
        back_populates="domain", cascade="all, delete-orphan"
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "domain", name="uq_domains_tenant_domain"),
    )


class DomainHealthHistory(TenantOwnedMixin, Base):
    __tablename__ = "domain_health_history"
    domain_id: Mapped[UUID] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), nullable=False
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    summary: Mapped[str] = mapped_column(Text, nullable=False)
    checks: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    domain: Mapped[Domain] = relationship(back_populates="history")
    __table_args__ = (Index("ix_domain_health_history_tenant_domain", "tenant_id", "domain_id"),)


class DomainCheck(TenantOwnedMixin, Base):
    __tablename__ = "domain_checks"
    domain_id: Mapped[UUID] = mapped_column(
        ForeignKey("domains.id", ondelete="CASCADE"), nullable=False
    )
    check_type: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(20), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    remediation: Mapped[str | None] = mapped_column(Text)
    checked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    domain_record: Mapped[Domain] = relationship(back_populates="checks")
    __table_args__ = (Index("ix_domain_checks_tenant_type", "tenant_id", "check_type"),)


class Template(TenantOwnedMixin, Base):
    __tablename__ = "templates"
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str | None] = mapped_column(String(500))
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    created_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    current_version_id: Mapped[UUID | None] = mapped_column(ForeignKey("template_versions.id", ondelete="SET NULL"))
    versions: Mapped[list[TemplateVersion]] = relationship(
        back_populates="template", cascade="all, delete-orphan", foreign_keys="TemplateVersion.template_id"
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "name", name="uq_templates_tenant_name"),
    )


class TemplateVersion(TenantOwnedMixin, Base):
    __tablename__ = "template_versions"
    template_id: Mapped[UUID] = mapped_column(
        ForeignKey("templates.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    subject_template: Mapped[str] = mapped_column(String(998), nullable=False)
    html_body: Mapped[str] = mapped_column(Text, nullable=False)
    text_body: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    created_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    variable_manifest: Mapped[list[str]] = mapped_column(
        JSONType, default=list, nullable=False
    )
    template: Mapped[Template] = relationship(back_populates="versions", foreign_keys=[template_id])
    variables: Mapped[list[TemplateVariable]] = relationship(
        back_populates="template_version", cascade="all, delete-orphan"
    )
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "template_id",
            "version_number",
            name="uq_template_version_number",
        ),
    )


class AIMessageDraft(TenantOwnedMixin, Base):
    """Production AI Message Studio draft.

    Objective -> recipient context -> AI generation -> draft -> human review ->
    approved content -> campaign (compliance, scheduling, sending are separate
    engines). Draft content is never sent directly; it must be human-approved
    before it can be used by a campaign, and it becomes a reusable Template
    only via an explicit save-as-template action.
    """

    __tablename__ = "ai_message_drafts"
    created_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    reviewed_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    campaign_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    contact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    template_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("templates.id", ondelete="SET NULL")
    )
    approved_template_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("templates.id", ondelete="SET NULL")
    )
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    input_context: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    generated_subject: Mapped[str] = mapped_column(String(998), nullable=False)
    generated_body: Mapped[str] = mapped_column(Text, nullable=False)
    generation_status: Mapped[str] = mapped_column(
        String(30), default="DRAFT", nullable=False
    )
    generation_type: Mapped[str] = mapped_column(
        String(40), default="INITIAL_EMAIL", nullable=False
    )
    tone: Mapped[str] = mapped_column(
        String(40), default="PROFESSIONAL", nullable=False
    )
    language: Mapped[str] = mapped_column(
        String(50), default="English", nullable=False
    )
    desired_length: Mapped[str] = mapped_column(
        String(20), default="MEDIUM", nullable=False
    )
    cta: Mapped[str] = mapped_column(
        String(2000), default="", nullable=False
    )
    provider: Mapped[str] = mapped_column(
        String(100), default="MOCK_AI", nullable=False
    )
    model: Mapped[str | None] = mapped_column(String(100))
    generation_method: Mapped[str] = mapped_column(
        String(30), default="PROVIDER", nullable=False
    )
    warnings: Mapped[list[Any]] = mapped_column(JSONType, default=list, nullable=False)
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    cost_currency: Mapped[str] = mapped_column(
        String(3), default="USD", nullable=False
    )
    request_duration_ms: Mapped[int | None] = mapped_column(Integer)
    error_message: Mapped[str | None] = mapped_column(String(1000))
    review_note: Mapped[str | None] = mapped_column(String(1000))
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index(
            "ix_ai_message_drafts_tenant_created",
            "tenant_id",
            "created_at",
        ),
        Index("ix_ai_message_drafts_tenant_status", "tenant_id", "generation_status"),
    )


class TemplateVariable(TenantOwnedMixin, Base):
    __tablename__ = "template_variables"
    template_version_id: Mapped[UUID] = mapped_column(
        ForeignKey("template_versions.id", ondelete="CASCADE"), nullable=False
    )
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    value_type: Mapped[str] = mapped_column(String(30), default="TEXT", nullable=False)
    required: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    template_version: Mapped[TemplateVersion] = relationship(back_populates="variables")
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "template_version_id", "name", name="uq_template_variable_name"
        ),
    )


class Campaign(TenantOwnedMixin, Base):
    __tablename__ = "campaigns"
    name: Mapped[str] = mapped_column(String(200), nullable=False)
    objective: Mapped[str] = mapped_column(Text, nullable=False)
    description: Mapped[str | None] = mapped_column(String(2000))
    sender_id: Mapped[UUID] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    template_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("template_versions.id", ondelete="RESTRICT")
    )
    recipient_list_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("contact_lists.id", ondelete="SET NULL")
    )
    segment_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("contact_segments.id", ondelete="SET NULL")
    )
    timezone: Mapped[str] = mapped_column(String(100), default="UTC", nullable=False)
    scheduled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    schedule_config: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    created_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # --- Normalized compliance state (guardrail 23) ------------------------
    # Re-computed server-side by ComplianceStatusService on every validate /
    # approve and before any schedule transition that can start sending.
    compliance_status: Mapped[str] = mapped_column(
        String(30), default="UNKNOWN", nullable=False
    )  # COMPLIANT | WARNING | BLOCKED | REVIEW_REQUIRED | UNKNOWN
    compliance_reasons: Mapped[list[str]] = mapped_column(
        JSONType, default=list, nullable=False
    )
    compliance_evaluated_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    versions: Mapped[list[CampaignVersion]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    recipients: Mapped[list[CampaignRecipient]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    campaign_senders: Mapped[list[CampaignSender]] = relationship(
        back_populates="campaign", cascade="all, delete-orphan"
    )
    __table_args__ = (Index("ix_campaigns_tenant_status", "tenant_id", "status"),)


class CampaignVersion(TenantOwnedMixin, Base):
    __tablename__ = "campaign_versions"
    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    version_number: Mapped[int] = mapped_column(Integer, nullable=False)
    snapshot: Mapped[dict[str, Any]] = mapped_column(JSONType, nullable=False)
    is_immutable: Mapped[bool] = mapped_column(Boolean, default=False, nullable=False)
    campaign: Mapped[Campaign] = relationship(back_populates="versions")
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "campaign_id",
            "version_number",
            name="uq_campaign_version_number",
        ),
    )


class CampaignRecipient(TenantOwnedMixin, Base):
    __tablename__ = "campaign_recipients"
    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    contact_id: Mapped[UUID] = mapped_column(
        ForeignKey("contacts.id", ondelete="RESTRICT"), nullable=False
    )
    eligibility_status: Mapped[str] = mapped_column(
        String(30), default="PENDING", nullable=False
    )
    rendered_data: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    campaign: Mapped[Campaign] = relationship(back_populates="recipients")
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "campaign_id", "contact_id", name="uq_campaign_recipient"
        ),
    )


class CampaignSender(TenantOwnedMixin, Base):
    """Many-to-many campaign ⇄ sender assignment (Phase 10Q).

    A campaign sends through one or more sender accounts. All references are
    tenant-scoped; both sides are validated for tenant membership by the
    service layer (same pattern as every other tenant-owned association).

    ``daily_limit`` defaults to NULL = inherit the sender's
    ``daily_campaign_limit``; otherwise it caps this campaign's daily spend
    on that sender.
    """

    __tablename__ = "campaign_senders"
    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    sender_id: Mapped[UUID] = mapped_column(
        ForeignKey("sender_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    daily_limit: Mapped[int | None] = mapped_column(Integer)  # NULL = inherit sender cap
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    campaign: Mapped[Campaign] = relationship(back_populates="campaign_senders")
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "campaign_id", "sender_id", name="uq_campaign_sender"
        ),
        Index("ix_campaign_senders_tenant_sender", "tenant_id", "sender_id"),
    )


class ScheduledMessage(TenantOwnedMixin, Base):
    __tablename__ = "scheduled_messages"
    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    campaign_recipient_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaign_recipients.id", ondelete="CASCADE"), nullable=False
    )
    due_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    idempotency_key: Mapped[str] = mapped_column(
        String(255), nullable=False, unique=True
    )
    deferred_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failure_reason: Mapped[str | None] = mapped_column(Text)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        Index("ix_scheduled_messages_queue", "status", "due_at", "priority"),
    )


class DeliveryJob(TenantOwnedMixin, Base):
    """Durable, concurrency-safe delivery queue.

    One row per recipient per campaign (idempotent via the tenant/campaign/
    recipient unique constraint). Jobs are claimed atomically by workers under
    a lease, and retries are scheduled only for transient failures via
    ``next_attempt_at`` with exponential backoff.
    """

    __tablename__ = "delivery_jobs"

    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    campaign_version_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("campaign_versions.id", ondelete="SET NULL")
    )
    recipient_id: Mapped[UUID] = mapped_column(
        ForeignKey("contacts.id", ondelete="RESTRICT"), nullable=False
    )
    sender_id: Mapped[UUID] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    sender_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("sender_accounts.id", ondelete="RESTRICT")
    )
    scheduled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    status: Mapped[str] = mapped_column(String(30), default="PENDING", nullable=False)
    attempt_count: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    max_attempts: Mapped[int] = mapped_column(Integer, default=5, nullable=False)
    last_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider_message_id: Mapped[str | None] = mapped_column(String(500))
    failure_code: Mapped[str | None] = mapped_column(String(100))
    last_error: Mapped[str | None] = mapped_column(Text)
    lease_owner: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    processing_started_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "campaign_id", "recipient_id", name="uq_delivery_job_recipient"
        ),
        Index("ix_delivery_jobs_discovery", "status", "next_attempt_at"),
        Index("ix_delivery_jobs_tenant_campaign", "tenant_id", "campaign_id", "status"),
        Index("ix_delivery_jobs_sender_account_id", "sender_account_id"),
    )


class Message(TenantOwnedMixin, Base):
    __tablename__ = "messages"
    scheduled_message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("scheduled_messages.id", ondelete="SET NULL")
    )
    campaign_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    sender_id: Mapped[UUID] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="RESTRICT"), nullable=False
    )
    sender_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("sender_accounts.id", ondelete="RESTRICT")
    )
    contact_id: Mapped[UUID] = mapped_column(
        ForeignKey("contacts.id", ondelete="RESTRICT"), nullable=False
    )
    # Traffic class (System B): CAMPAIGN | WARMUP | TEST. Keeps the durable
    # per-send log shared while allowing strict queue separation upstream.
    msg_type: Mapped[str] = mapped_column(String(20), default="CAMPAIGN", nullable=False)
    recipient: Mapped[str | None] = mapped_column(String(320))
    message_id: Mapped[str | None] = mapped_column(String(998))  # RFC 5322 Message-ID header
    provider_message_id: Mapped[str | None] = mapped_column(String(500))
    provider_thread_id: Mapped[str | None] = mapped_column(String(500))
    subject: Mapped[str] = mapped_column(String(998), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="QUEUED", nullable=False)
    queued_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    sent_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    error_code: Mapped[str | None] = mapped_column(String(100))
    error_message: Mapped[str | None] = mapped_column(Text)
    events: Mapped[list[MessageEvent]] = relationship(
        back_populates="message", cascade="all, delete-orphan"
    )
    __table_args__ = (
        Index("ix_messages_tenant_status", "tenant_id", "status"),
        Index("ix_messages_tenant_sender_account", "tenant_id", "sender_account_id"),
        Index("ix_messages_message_id", "message_id"),
        UniqueConstraint("tenant_id", "scheduled_message_id", name="uq_messages_scheduled_message"),
    )


class MessageEvent(TenantOwnedMixin, Base):
    __tablename__ = "message_events"
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    sender_account_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("sender_accounts.id", ondelete="RESTRICT")
    )
    event_type: Mapped[str] = mapped_column(String(40), nullable=False)
    provider_event_id: Mapped[str | None] = mapped_column(String(500))
    provider_payload: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    message: Mapped[Message] = relationship(back_populates="events")
    __table_args__ = (
        Index("ix_message_events_tenant_sender", "tenant_id", "sender_account_id"),
        UniqueConstraint(
            "tenant_id", "provider_event_id", name="uq_message_events_provider_id"
        ),
    )


class Thread(TenantOwnedMixin, Base):
    __tablename__ = "threads"
    provider_thread_id: Mapped[str] = mapped_column(String(500), nullable=False)
    sender_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="SET NULL")
    )
    contact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    campaign_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    status: Mapped[str] = mapped_column(String(30), default="OPEN", nullable=False)
    subject: Mapped[str | None] = mapped_column(String(998))
    assigned_user_id: Mapped[UUID | None] = mapped_column(ForeignKey("users.id", ondelete="SET NULL"))
    notes: Mapped[str | None] = mapped_column(Text)
    tags: Mapped[list[str]] = mapped_column(JSONType, default=list, nullable=False)
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    replies: Mapped[list[Reply]] = relationship(
        back_populates="thread", cascade="all, delete-orphan"
    )
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider_thread_id", name="uq_threads_provider_id"
        ),
    )


class Reply(TenantOwnedMixin, Base):
    __tablename__ = "replies"
    thread_id: Mapped[UUID] = mapped_column(
        ForeignKey("threads.id", ondelete="CASCADE"), nullable=False
    )
    message_id: Mapped[UUID | None] = mapped_column(ForeignKey("messages.id", ondelete="SET NULL"))
    sender_email: Mapped[str | None] = mapped_column(String(320))
    recipient_email: Mapped[str | None] = mapped_column(String(320))
    body_html: Mapped[str | None] = mapped_column(Text)
    provider_message_id: Mapped[str | None] = mapped_column(String(500))
    body_text: Mapped[str | None] = mapped_column(Text)
    classification: Mapped[str | None] = mapped_column(String(40))
    suggested_action: Mapped[str | None] = mapped_column(String(80))
    suggested_response: Mapped[str | None] = mapped_column(Text)
    approval_status: Mapped[str] = mapped_column(
        String(30), default="PENDING", nullable=False
    )
    received_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    thread: Mapped[Thread] = relationship(back_populates="replies")
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "provider_message_id", name="uq_replies_provider_id"
        ),
    )


class EmailThread(TenantOwnedMixin, Base):
    """A conversation thread in the unified inbox (Phase 17)."""

    __tablename__ = "email_threads"
    sender_id: Mapped[UUID] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="CASCADE"), nullable=False
    )
    external_thread_id: Mapped[str] = mapped_column(String(500), nullable=False)
    subject: Mapped[str | None] = mapped_column(String(998))
    last_message_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(
        String(30), default="UNREAD", nullable=False
    )
    # Association (best-effort; never guessed when confidence is low).
    contact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    campaign_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    match_status: Mapped[str] = mapped_column(
        String(30), default="UNMATCHED", nullable=False
    )
    assigned_user_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    messages: Mapped[list[EmailMessage]] = relationship(
        back_populates="thread",
        cascade="all, delete-orphan",
        order_by="EmailMessage.received_at",
    )
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "sender_id",
            "external_thread_id",
            name="uq_email_threads_provider",
        ),
        Index("ix_email_threads_tenant_status", "tenant_id", "status"),
    )


class EmailMessage(TenantOwnedMixin, Base):
    """A single message within an inbox thread (Phase 17)."""

    __tablename__ = "email_messages"
    thread_id: Mapped[UUID] = mapped_column(
        ForeignKey("email_threads.id", ondelete="CASCADE"), nullable=False
    )
    external_message_id: Mapped[str] = mapped_column(String(500), nullable=False)
    direction: Mapped[str] = mapped_column(String(20), nullable=False)
    from_email: Mapped[str] = mapped_column(String(320), nullable=False)
    to_email: Mapped[str] = mapped_column(String(320), nullable=False)
    subject: Mapped[str] = mapped_column(String(998), nullable=False)
    body_reference: Mapped[str | None] = mapped_column(String(500))
    body_text: Mapped[str | None] = mapped_column(Text)
    body_html: Mapped[str | None] = mapped_column(Text)
    received_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    status: Mapped[str] = mapped_column(String(30), default="RECEIVED", nullable=False)
    in_reply_to: Mapped[str | None] = mapped_column(String(500))
    references: Mapped[str | None] = mapped_column(Text)
    thread: Mapped[EmailThread] = relationship(back_populates="messages")
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "external_message_id", name="uq_email_messages_provider"
        ),
        Index("ix_email_messages_tenant_thread", "tenant_id", "thread_id"),
    )


class AIReplyDraft(TenantOwnedMixin, Base):
    """An AI-generated reply that is NEVER sent automatically (Phase 17).

    The user must review, edit, and approve a draft before anything is sent.
    Phase 18 extends it to carry assistant operation metadata, intent, and
    safety warnings; the AI likewise never auto-sends.
    """

    __tablename__ = "ai_reply_drafts"
    thread_id: Mapped[UUID] = mapped_column(
        ForeignKey("email_threads.id", ondelete="CASCADE"), nullable=False
    )
    created_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    subject: Mapped[str] = mapped_column(String(998), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    status: Mapped[str] = mapped_column(
        String(30), default="DRAFT", nullable=False
    )  # DRAFT | APPROVED | SENT | FAILED | REJECTED
    provider: Mapped[str] = mapped_column(String(30), default="MOCK", nullable=False)
    model: Mapped[str | None] = mapped_column(String(120))
    input_tokens: Mapped[int | None] = mapped_column(Integer)
    output_tokens: Mapped[int | None] = mapped_column(Integer)
    approved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    # Phase 18: assistant operation and intent metadata.
    operation: Mapped[str] = mapped_column(
        String(30), default="DRAFT", nullable=False
    )  # DRAFT | SUMMARIZE | IDENTIFY_INTENT | NEXT_ACTION | PROFESSIONAL | SHORTEN | EXPAND | CHANGE_TONE | TRANSLATE
    intent: Mapped[str | None] = mapped_column(String(40))
    intent_confidence: Mapped[float | None] = mapped_column(Float)
    warnings: Mapped[str | None] = mapped_column(Text)  # JSON array
    summary: Mapped[str | None] = mapped_column(Text)
    next_action: Mapped[str | None] = mapped_column(Text)
    tone: Mapped[str | None] = mapped_column(String(40))
    language: Mapped[str | None] = mapped_column(String(40))
    source_draft_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("ai_reply_drafts.id", ondelete="SET NULL")
    )
    rejected_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    rejected_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (Index("ix_ai_reply_drafts_tenant_thread", "tenant_id", "thread_id"),)


class Suppression(TenantOwnedMixin, Base):
    __tablename__ = "suppressions"
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    contact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    reason: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    effective_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", "email", name="uq_suppressions_tenant_email"),
    )


class Unsubscribe(TenantOwnedMixin, Base):
    __tablename__ = "unsubscribes"
    email: Mapped[str] = mapped_column(String(320), nullable=False)
    contact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    token_hash: Mapped[str] = mapped_column(String(255), nullable=False, unique=True)
    campaign_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    unsubscribed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    __table_args__ = (Index("ix_unsubscribes_tenant_email", "tenant_id", "email"),)


class Bounce(TenantOwnedMixin, Base):
    __tablename__ = "bounces"
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    classification: Mapped[str] = mapped_column(String(40), nullable=False)
    diagnostic: Mapped[str | None] = mapped_column(Text)
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class Complaint(TenantOwnedMixin, Base):
    __tablename__ = "complaints"
    message_id: Mapped[UUID] = mapped_column(
        ForeignKey("messages.id", ondelete="CASCADE"), nullable=False
    )
    provider_reference: Mapped[str | None] = mapped_column(String(500))
    occurred_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )


class SuppressionEntry(TenantOwnedMixin, Base):
    """Authoritative per-tenant suppression safety control (Phase 14).

    Every outgoing message MUST be checked against this table immediately
    before delivery. No campaign, scheduler, worker, provider adapter, or
    retry mechanism may bypass it. Rows are derived from provider events
    (hard bounce, complaint, unsubscribe) or explicit operator actions
    (MANUAL / ADMIN_BLOCKED). Provider-derived entries (COMPLAINT,
    HARD_BOUNCE) are protected from silent removal.
    """

    __tablename__ = "suppression_entries"
    email_normalized: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    type: Mapped[str] = mapped_column(String(30), nullable=False)
    source: Mapped[str] = mapped_column(String(100), nullable=False)
    reason: Mapped[str | None] = mapped_column(String(500))
    provider: Mapped[str | None] = mapped_column(String(30))
    campaign_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    contact_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("contacts.id", ondelete="SET NULL")
    )
    active: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "email_normalized", name="uq_suppression_entries_tenant_email"
        ),
        Index("ix_suppression_entries_tenant_type", "tenant_id", "type"),
        Index("ix_suppression_entries_tenant_created", "tenant_id", "created_at"),
    )


class NormalizedDeliveryEvent(TenantOwnedMixin, Base):
    """Provider-agnostic normalized delivery event (Phase 14).

    Stored idempotently keyed on (tenant_id, provider, provider_event_id) so a
    provider may deliver the same event multiple times without duplicating
    suppression records, analytics, campaign statistics, or audit entries.
    """

    __tablename__ = "normalized_delivery_events"
    provider: Mapped[str] = mapped_column(String(30), nullable=False)
    provider_event_id: Mapped[str] = mapped_column(String(500), nullable=False)
    message_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("messages.id", ondelete="SET NULL")
    )
    recipient: Mapped[str] = mapped_column(String(320), nullable=False)
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    event_time: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    raw_reference: Mapped[str | None] = mapped_column(String(500))
    processed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider",
            "provider_event_id",
            name="uq_normalized_delivery_events_provider_event",
        ),
        Index("ix_normalized_delivery_events_tenant_msg", "tenant_id", "message_id"),
        Index("ix_normalized_delivery_events_tenant_email", "tenant_id", "recipient"),
    )


class ComplianceResult(TenantOwnedMixin, Base):
    """Per-check result of the compliance engine for a message.

    Deliberately holds no recipient personal information (no email, no
    message text) -- only a non-PII check_type, result, and reason.
    """

    __tablename__ = "compliance_results"
    campaign_id: Mapped[UUID] = mapped_column(
        ForeignKey("campaigns.id", ondelete="CASCADE"), nullable=False
    )
    recipient_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("campaign_recipients.id", ondelete="CASCADE")
    )
    sender_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("email_accounts.id", ondelete="SET NULL")
    )
    check_type: Mapped[str] = mapped_column(String(100), nullable=False)
    result: Mapped[str] = mapped_column(
        String(20), default="PASS", nullable=False
    )
    reason: Mapped[str] = mapped_column(Text, default="", nullable=False)
    check_source: Mapped[str] = mapped_column(
        String(30), default="SEND", nullable=False
    )
    __table_args__ = (
        Index("ix_compliance_results_tenant_campaign", "tenant_id", "campaign_id"),
        Index("ix_compliance_results_tenant_created", "tenant_id", "created_at"),
        Index(
            "ix_compliance_results_tenant_recipient",
            "tenant_id",
            "recipient_id",
            "check_type",
        ),
    )


class AIGeneration(TenantOwnedMixin, Base):
    __tablename__ = "ai_generations"
    created_by_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    campaign_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("campaigns.id", ondelete="SET NULL")
    )
    provider: Mapped[str] = mapped_column(String(100), nullable=False)
    model: Mapped[str | None] = mapped_column(String(100))
    request_summary: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    generated_output: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    validation_status: Mapped[str] = mapped_column(
        String(30), default="PENDING", nullable=False
    )
    workflow_status: Mapped[str] = mapped_column(String(30), default="DRAFT", nullable=False)
    prompt_tokens: Mapped[int | None] = mapped_column(Integer)
    completion_tokens: Mapped[int | None] = mapped_column(Integer)
    total_tokens: Mapped[int | None] = mapped_column(Integer)
    estimated_cost: Mapped[Decimal | None] = mapped_column(Numeric(18, 8))
    cost_currency: Mapped[str] = mapped_column(String(3), default="USD", nullable=False)


class AuditLog(TenantOwnedMixin, Base):
    __tablename__ = "audit_logs"
    actor_id: Mapped[UUID | None] = mapped_column(
        ForeignKey("users.id", ondelete="SET NULL")
    )
    action: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_type: Mapped[str] = mapped_column(String(100), nullable=False)
    resource_id: Mapped[UUID | None] = mapped_column()
    request_id: Mapped[str | None] = mapped_column(String(100))
    audit_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, default=dict, nullable=False
    )
    __table_args__ = (Index("ix_audit_logs_tenant_created", "tenant_id", "created_at"),)


class Webhook(TenantOwnedMixin, Base):
    __tablename__ = "webhooks"
    provider: Mapped[str] = mapped_column(String(50), nullable=False)
    external_event_id: Mapped[str] = mapped_column(String(500), nullable=False)
    signature_valid: Mapped[bool] = mapped_column(
        Boolean, default=False, nullable=False
    )
    processing_status: Mapped[str] = mapped_column(
        String(30), default="RECEIVED", nullable=False
    )
    payload: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    __table_args__ = (
        UniqueConstraint(
            "tenant_id",
            "provider",
            "external_event_id",
            name="uq_webhooks_external_event",
        ),
    )


class UsageRecord(TenantOwnedMixin, Base):
    __tablename__ = "usage_records"
    metric: Mapped[str] = mapped_column(String(100), nullable=False)
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    source: Mapped[str | None] = mapped_column(String(100))
    __table_args__ = (
        UniqueConstraint(
            "tenant_id", "metric", "period_start", name="uq_usage_record_period"
        ),
    )


class TenantSubscription(TenantOwnedMixin, Base):
    """The tenant's billing subscription.

    ``plan_code`` selects the feature/limit set from the (data-driven,
    provider-agnostic) plan catalog. ``custom_limits`` holds per-metric
    capacity overrides on top of the plan defaults. Explicit billing quotas
    never bypass suppression, compliance, provider, or security controls —
    those gates are enforced independently in the sending/suppression paths.
    """

    __tablename__ = "tenant_subscriptions"
    plan_code: Mapped[str] = mapped_column(
        String(30), default="free", nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(30), default="ACTIVE", nullable=False
    )
    seats: Mapped[int | None] = mapped_column(Integer)
    custom_limits: Mapped[dict[str, Any]] = mapped_column(
        JSONType, default=dict, nullable=False
    )
    period_start: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    period_end: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    __table_args__ = (
        UniqueConstraint("tenant_id", name="uq_tenant_subscriptions_tenant"),
    )


class UsageEvent(Base):
    """Immutable, append-only usage meter.

    Every row is a completed happening (contact created, AI generation,
    campaign created, message sent, sender connected, storage used, team
    member added). There are no update or delete paths: aggregation reads
    ``created_at``/``period_start`` and the row is written once inside the
    same transaction as the operation it meters.
    """

    __tablename__ = "usage_events"
    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    event_type: Mapped[str] = mapped_column(String(50), nullable=False)
    period_start: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    quantity: Mapped[Decimal] = mapped_column(
        Numeric(18, 4), nullable=False, default=Decimal("1")
    )
    resource_type: Mapped[str | None] = mapped_column(String(50))
    resource_id: Mapped[UUID | None] = mapped_column(Uuid(as_uuid=True))
    event_metadata: Mapped[dict[str, Any]] = mapped_column(
        "metadata", JSONType, default=dict, nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    __table_args__ = (
        Index(
            "ix_usage_events_tenant_period_type",
            "tenant_id",
            "period_start",
            "event_type",
        ),
    )


class AlertRecord(Base):
    """A fired or acknowledged operational alert.

    Alerts are platform-wide: ``tenant_id`` is set when the alert concerns a
    specific tenant and NULL for system-wide conditions (database, redis,
    worker, scheduler, disk). ``status`` is one of ``OPEN``,
    ``ACKNOWLEDGED``, or ``RESOLVED``; ``rule`` names the alert rule that
    produced it.
    """

    __tablename__ = "alert_records"
    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    tenant_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    rule: Mapped[str] = mapped_column(String(100), nullable=False)
    severity: Mapped[str] = mapped_column(String(20), default="warning", nullable=False)
    metric: Mapped[str | None] = mapped_column(String(100))
    status: Mapped[str] = mapped_column(String(20), default="OPEN", nullable=False)
    message: Mapped[str] = mapped_column(String(1000), nullable=False)
    details: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    acknowledged_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    acknowledged_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL")
    )
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    __table_args__ = (
        Index("ix_alert_records_tenant_status", "tenant_id", "status"),
        Index("ix_alert_records_tenant_created", "tenant_id", "created_at"),
    )


class OpsMetricSample(Base):
    """A periodic operations sample (queue depth, latency, heartbeat age).

    System-wide samples carry ``tenant_id`` NULL; tenant-scoped probes set it
    so the ops dashboard can attribute a sample.
    """

    __tablename__ = "ops_metric_samples"
    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    tenant_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    metric: Mapped[str] = mapped_column(String(100), nullable=False)
    value: Mapped[float] = mapped_column(Float, nullable=False)
    labels: Mapped[dict[str, Any]] = mapped_column(JSONType, default=dict, nullable=False)
    sampled_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    __table_args__ = (
        Index(
            "ix_ops_metric_samples_tenant_metric_sampled",
            "tenant_id",
            "metric",
            "sampled_at",
        ),
    )


class OutboundMessage(TenantOwnedMixin, Base):
    """A durable outbound email send record (Phase 8 #4/#6).

    Mirrors migration ``20260916_06`` (head). Created at queue time with
    ``status="QUEUED"``; the send state machine (``app.services.outbound.
    send_state_machine``) is the only path that mutates ``status``:

    .. code-block:: text

        QUEUED -> PROCESSING -> SENT
        QUEUED -> PROCESSING -> RETRYING -> PROCESSING -> SENT
        QUEUED -> PROCESSING -> FAILED
        QUEUED -> CANCELLED

    Idempotency: ``correlation_id`` is the app-level anchor with a unique
    ``(tenant_id, correlation_id)`` index; ``idempotency_key`` carries the
    per-provider idempotency key with a second unique index. Workers claim
    rows atomically (one ``UPDATE ... WHERE status='QUEUED' RETURNING``) so
    no two workers can process the same row.

    Security: this model intentionally stores **no credentials** — no access
    tokens, refresh tokens, API keys, or client secrets. It references the
    resolved ``provider_connection_id`` only so audit trails can attribute a
    send to a connection without ever persisting provider-sensitive data here.
    """

    __tablename__ = "outbound_messages"

    id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), primary_key=True, default=uuid4
    )
    tenant_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("tenants.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sender_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("senders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    provider_connection_id: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("provider_connections.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    provider: Mapped[str] = mapped_column(
        String(30), nullable=False, index=True
    )
    from_email: Mapped[str] = mapped_column(String(320), nullable=False)
    reply_to: Mapped[str | None] = mapped_column(String(320), nullable=True)
    to_recipients: Mapped[list[str]] = mapped_column(
        JSONType, nullable=False, default=list
    )
    cc_recipients: Mapped[list[str]] = mapped_column(
        JSONType, nullable=False, default=list
    )
    bcc_recipients: Mapped[list[str]] = mapped_column(
        JSONType, nullable=False, default=list
    )
    subject: Mapped[str] = mapped_column(String(998), nullable=False)
    text_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    html_body: Mapped[str | None] = mapped_column(Text, nullable=True)
    correlation_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True), default=uuid4, nullable=False
    )
    status: Mapped[str] = mapped_column(
        String(20), default="QUEUED", nullable=False
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, default=0, nullable=False
    )
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(String(1000), nullable=True)
    last_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    sent_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    failed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )
    provider_message_id: Mapped[str | None] = mapped_column(
        String(512), nullable=True
    )
    idempotency_key: Mapped[str | None] = mapped_column(
        String(255), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )
    __table_args__ = (
        Index(
            "ix_outbound_messages_tenant_status_created",
            "tenant_id",
            "status",
            "created_at",
        ),
        Index(
            "ix_outbound_messages_tenant_correlation",
            "tenant_id",
            "correlation_id",
            unique=True,
        ),
        Index(
            "ix_outbound_messages_tenant_idempotency",
            "tenant_id",
            "idempotency_key",
            unique=True,
        ),
    )

class SenderPool(TenantOwnedMixin, Base):
    """A tenant-scoped, reusable collection of senders for campaign routing.

    Pools are pure metadata containers: they hold **no credentials** (no
    tokens, refresh tokens, API keys, client secrets, or SMTP passwords) and
    no sender payloads -- only the tenant-local routing header. Membership and
    eligibility live in :class:`SenderPoolMember` rows. Phased-8 contract
    (no credentials ever) applies here exactly as it does to OutboundMessage.
    """

    __tablename__ = "sender_pools"

    name: Mapped[str] = mapped_column(String(120), nullable=False)
    description: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[str] = mapped_column(
        String(20), default="ACTIVE", nullable=False, index=True
    )
    created_by: Mapped[UUID | None] = mapped_column(
        Uuid(as_uuid=True), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index("ix_sender_pools_tenant_name", "tenant_id", "name", unique=True),
        Index("ix_sender_pools_tenant_status", "tenant_id", "status"),
    )


class SenderPoolMember(TenantOwnedMixin, Base):
    """Tracks one sender's membership in one tenant-scoped sender pool.

    The unique ``(tenant_id, sender_pool_id, sender_id)`` constraint prevents
    duplicate membership and cross-tenant pools. ``priority`` orders senders
    for round-robin/weighted selection; credentials are **never** stored here
    (reference-only, matching the OutboundMessage contract).
    """

    __tablename__ = "sender_pool_members"

    sender_pool_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("sender_pools.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    sender_id: Mapped[UUID] = mapped_column(
        Uuid(as_uuid=True),
        ForeignKey("senders.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(Boolean, default=True, nullable=False)
    priority: Mapped[int] = mapped_column(Integer, default=0, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), server_default=func.now(), nullable=False
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        server_default=func.now(),
        onupdate=func.now(),
        nullable=False,
    )

    __table_args__ = (
        Index(
            "ix_sender_pool_members_tenant_pool_sender",
            "tenant_id",
            "sender_pool_id",
            "sender_id",
            unique=True,
        ),
        Index("ix_sender_pool_members_tenant", "tenant_id"),
    )
