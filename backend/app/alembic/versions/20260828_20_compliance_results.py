"""Phase 8 Email Compliance Engine: compliance_results table.

Revision ID: 20260828_20
Revises: 20260828_19
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260828_20"
down_revision = "20260828_19"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return set(inspect(op.get_bind()).get_table_names())


def _columns(table: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def upgrade() -> None:
    if "compliance_results" in _tables():
        return
    op.create_table(
        "compliance_results",
        sa.Column("id", sa.Uuid(), primary_key=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("recipient_id", sa.Uuid(), nullable=True),
        sa.Column("sender_id", sa.Uuid(), nullable=True),
        sa.Column("check_type", sa.String(length=100), nullable=False),
        sa.Column(
            "result",
            sa.String(length=20),
            server_default="PASS",
            nullable=False,
        ),
        sa.Column("reason", sa.Text(), server_default="", nullable=False),
        sa.Column(
            "check_source",
            sa.String(length=30),
            server_default="SEND",
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["tenant_id"], ["tenants.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["campaign_id"], ["campaigns.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["recipient_id"], ["campaign_recipients.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["sender_id"], ["email_accounts.id"], ondelete="SET NULL"
        ),
        sa.Index("ix_compliance_results_tenant_campaign", "tenant_id", "campaign_id"),
        sa.Index("ix_compliance_results_tenant_created", "tenant_id", "created_at"),
        sa.Index(
            "ix_compliance_results_tenant_recipient",
            "tenant_id",
            "recipient_id",
            "check_type",
        ),
    )
    # Normalize any legacy "WARN" strings (should be none, but be safe).
    bind = op.get_bind()
    existing = _columns("compliance_results")
    if "result" in existing:
        bind.execute(
            sa.text(
                "UPDATE compliance_results SET result = 'WARNING' "
                "WHERE result = 'WARN'"
            )
        )


def downgrade() -> None:
    if "compliance_results" not in _tables():
        return
    op.drop_index("ix_compliance_results_tenant_campaign", table_name="compliance_results")
    op.drop_index("ix_compliance_results_tenant_created", table_name="compliance_results")
    op.drop_index("ix_compliance_results_tenant_recipient", table_name="compliance_results")
    op.drop_table("compliance_results")
