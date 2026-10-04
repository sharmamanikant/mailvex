"""Phase 15 Campaign Scheduler + Delivery Queue: durable delivery_jobs table.

Revision ID: 20260830_23
Revises: 20260829_22
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260830_23"
down_revision = "20260829_22"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return {table for table in inspect(op.get_bind()).get_table_names()}


def upgrade() -> None:
    if "delivery_jobs" in _tables():
        return
    op.create_table(
        "delivery_jobs",
        sa.Column("id", sa.Uuid(), nullable=False),
        sa.Column("tenant_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_id", sa.Uuid(), nullable=False),
        sa.Column("campaign_version_id", sa.Uuid(), nullable=True),
        sa.Column("recipient_id", sa.Uuid(), nullable=False),
        sa.Column("sender_id", sa.Uuid(), nullable=False),
        sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=30), server_default="PENDING", nullable=False),
        sa.Column("attempt_count", sa.Integer(), server_default="0", nullable=False),
        sa.Column("max_attempts", sa.Integer(), server_default="5", nullable=False),
        sa.Column("last_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("next_attempt_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("provider_message_id", sa.String(length=500), nullable=True),
        sa.Column("failure_code", sa.String(length=100), nullable=True),
        sa.Column("last_error", sa.Text(), nullable=True),
        sa.Column("lease_owner", sa.Uuid(), nullable=True),
        sa.Column("lease_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("processing_started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
        sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="CASCADE"),
        sa.ForeignKeyConstraint(["campaign_version_id"], ["campaign_versions.id"], ondelete="SET NULL"),
        sa.ForeignKeyConstraint(["recipient_id"], ["contacts.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["sender_id"], ["email_accounts.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id", name="pk_delivery_jobs"),
        sa.UniqueConstraint("tenant_id", "campaign_id", "recipient_id", name="uq_delivery_job_recipient"),
    )
    op.create_index(
        "ix_delivery_jobs_discovery",
        "delivery_jobs",
        ["status", "next_attempt_at"],
    )
    op.create_index(
        "ix_delivery_jobs_tenant_campaign",
        "delivery_jobs",
        ["tenant_id", "campaign_id", "status"],
    )


def downgrade() -> None:
    if "delivery_jobs" in _tables():
        op.drop_table("delivery_jobs")
