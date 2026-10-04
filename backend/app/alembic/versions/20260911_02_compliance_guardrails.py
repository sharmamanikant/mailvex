"""Compliance guardrails: policy + profile tables, consent/compliance columns.

Adds:
* ``compliance_policies`` — versioned ToS / Acceptable-Use documents.
* ``policy_acceptances`` — per-tenant/user recorded acceptance.
* ``compliance_profiles`` — configurable tenant compliance settings + safety
  thresholds (jurisdiction, required gates, retention policy).
* ``contacts`` consent/lawful-basis metadata columns.
* ``campaigns`` + ``sender_accounts`` normalized compliance state columns.

Revision ID: 20260911_02
Revises: 20260911_01
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260911_02"
down_revision = "20260911_01"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return {table for table in inspect(op.get_bind()).get_table_names()}


def _columns(table: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    present = _tables()

    if "compliance_policies" not in present:
        op.create_table(
            "compliance_policies",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("policy_type", sa.String(length=40), nullable=False),
            sa.Column("policy_version", sa.String(length=20), nullable=False),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("summary", sa.String(length=2000), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.PrimaryKeyConstraint("id", name="pk_compliance_policies"),
            sa.UniqueConstraint(
                "policy_type", "policy_version", name="uq_compliance_policies_type_version"
            ),
        )
        op.create_index(
            "ix_compliance_policies_type_version",
            "compliance_policies",
            ["policy_type", "policy_version"],
        )

    if "policy_acceptances" not in present:
        op.create_table(
            "policy_acceptances",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("user_id", sa.Uuid(), nullable=False),
            sa.Column("policy_type", sa.String(length=40), nullable=False),
            sa.Column("policy_version", sa.String(length=20), nullable=False),
            sa.Column("accepted_at", sa.DateTime(timezone=True), nullable=False),
            sa.Column("ip_address", sa.String(length=64), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id", name="pk_policy_acceptances"),
            sa.UniqueConstraint(
                "tenant_id", "user_id", "policy_type", name="uq_policy_acceptance_user_type"
            ),
        )
        op.create_index(
            "ix_policy_acceptances_tenant_user",
            "policy_acceptances",
            ["tenant_id", "user_id"],
        )

    if "compliance_profiles" not in present:
        op.create_table(
            "compliance_profiles",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("compliance_profile", sa.String(length=50), server_default="STANDARD", nullable=False),
            sa.Column("jurisdiction", sa.String(length=100), server_default="UNSPECIFIED", nullable=False),
            sa.Column("require_unsubscribe", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("require_sender_identity", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("require_policy_acceptance", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("require_consent_metadata", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("require_list_unsubscribe_header", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("retention_policy", sa.JSON(), nullable=False),
            sa.Column("safety_thresholds", sa.JSON(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id", name="pk_compliance_profiles"),
            sa.UniqueConstraint("tenant_id", name="uq_compliance_profile_tenant"),
        )

    if "contacts" in present:
        contact_columns = _columns("contacts")
        additions = {
            "consent_status": sa.String(length=30),
            "consent_source": sa.String(length=100),
            "consent_evidence": sa.String(length=500),
            "consent_timestamp": sa.DateTime(timezone=True),
            "unsubscribe_status": sa.String(length=30),
            "unsubscribed_at": sa.DateTime(timezone=True),
            "suppressed_at": sa.DateTime(timezone=True),
            "suppression_reason": sa.String(length=100),
        }
        for name, column_type in additions.items():
            if name not in contact_columns:
                op.add_column("contacts", sa.Column(name, column_type, nullable=True))
        op.alter_column(
            "contacts", "consent_status",
            existing_type=sa.String(length=30),
            server_default="UNKNOWN",
            existing_nullable=True,
        )
        op.alter_column(
            "contacts", "unsubscribe_status",
            existing_type=sa.String(length=30),
            server_default="NONE",
            existing_nullable=True,
        )

    if "campaigns" in present:
        campaign_columns = _columns("campaigns")
        for name, column_type in {
            "compliance_status": sa.String(length=30),
            "compliance_reasons": sa.JSON(),
            "compliance_evaluated_at": sa.DateTime(timezone=True),
        }.items():
            if name not in campaign_columns:
                op.add_column("campaigns", sa.Column(name, column_type, nullable=True))
        op.alter_column(
            "campaigns", "compliance_status",
            existing_type=sa.String(length=30),
            server_default="UNKNOWN",
            existing_nullable=True,
        )

    if "sender_accounts" in present:
        sender_columns = _columns("sender_accounts")
        for name, column_type in {
            "compliance_status": sa.String(length=30),
            "compliance_reasons": sa.JSON(),
            "compliance_evaluated_at": sa.DateTime(timezone=True),
            "paused_at": sa.DateTime(timezone=True),
            "resume_guard": sa.Boolean(),
        }.items():
            if name not in sender_columns:
                op.add_column("sender_accounts", sa.Column(name, column_type, nullable=True))
        op.alter_column(
            "sender_accounts", "compliance_status",
            existing_type=sa.String(length=30),
            server_default="COMPLIANT",
            existing_nullable=True,
        )
        op.alter_column(
            "sender_accounts", "resume_guard",
            existing_type=sa.Boolean(),
            server_default=sa.text("false"),
            existing_nullable=True,
        )


def downgrade() -> None:
    present = _tables()
    if "sender_accounts" in present:
        for name in (
            "resume_guard",
            "paused_at",
            "compliance_evaluated_at",
            "compliance_reasons",
            "compliance_status",
        ):
            op.drop_column("sender_accounts", name)
    if "campaigns" in present:
        for name in ("compliance_evaluated_at", "compliance_reasons", "compliance_status"):
            op.drop_column("campaigns", name)
    if "contacts" in present:
        for name in (
            "suppression_reason",
            "suppressed_at",
            "unsubscribed_at",
            "unsubscribe_status",
            "consent_evidence",
            "consent_timestamp",
            "consent_source",
            "consent_status",
        ):
            op.drop_column("contacts", name)
    if "compliance_profiles" in present:
        op.drop_table("compliance_profiles")
    if "policy_acceptances" in present:
        op.drop_table("policy_acceptances")
    if "compliance_policies" in present:
        op.drop_table("compliance_policies")