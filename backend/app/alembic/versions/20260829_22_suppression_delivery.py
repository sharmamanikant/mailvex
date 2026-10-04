"""Phase 14 Delivery Events + Suppression Engine: new safety-control tables.

Revision ID: 20260829_22
Revises: 20260828_21
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260829_22"
down_revision = "20260828_21"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return {table for table in inspect(op.get_bind()).get_table_names()}


def upgrade() -> None:
    present = _tables()
    if "suppression_entries" not in present:
        op.create_table(
            "suppression_entries",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("email_normalized", sa.String(length=320), nullable=False),
            sa.Column("type", sa.String(length=30), nullable=False),
            sa.Column("source", sa.String(length=100), nullable=False),
            sa.Column("reason", sa.String(length=500), nullable=True),
            sa.Column("provider", sa.String(length=30), nullable=True),
            sa.Column("campaign_id", sa.Uuid(), nullable=True),
            sa.Column("contact_id", sa.Uuid(), nullable=True),
            sa.Column("active", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id", name="pk_suppression_entries"),
            sa.UniqueConstraint(
                "tenant_id",
                "email_normalized",
                name="uq_suppression_entries_tenant_email",
            ),
        )
        op.create_index(
            "ix_suppression_entries_tenant_email_normalized",
            "suppression_entries",
            ["email_normalized"],
        )
        op.create_index(
            "ix_suppression_entries_tenant_type",
            "suppression_entries",
            ["tenant_id", "type"],
        )
        op.create_index(
            "ix_suppression_entries_tenant_created",
            "suppression_entries",
            ["tenant_id", "created_at"],
        )
    if "normalized_delivery_events" not in present:
        op.create_table(
            "normalized_delivery_events",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("provider", sa.String(length=30), nullable=False),
            sa.Column("provider_event_id", sa.String(length=500), nullable=False),
            sa.Column("message_id", sa.Uuid(), nullable=True),
            sa.Column("recipient", sa.String(length=320), nullable=False),
            sa.Column("event_type", sa.String(length=50), nullable=False),
            sa.Column("event_time", sa.DateTime(timezone=True), nullable=True),
            sa.Column("raw_reference", sa.String(length=500), nullable=True),
            sa.Column("processed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["message_id"], ["messages.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id", name="pk_normalized_delivery_events"),
            sa.UniqueConstraint(
                "tenant_id",
                "provider",
                "provider_event_id",
                name="uq_normalized_delivery_events_provider_event",
            ),
        )
        op.create_index(
            "ix_normalized_delivery_events_tenant_msg",
            "normalized_delivery_events",
            ["tenant_id", "message_id"],
        )
        op.create_index(
            "ix_normalized_delivery_events_tenant_email",
            "normalized_delivery_events",
            ["tenant_id", "recipient"],
        )


def downgrade() -> None:
    present = _tables()
    if "normalized_delivery_events" in present:
        op.drop_table("normalized_delivery_events")
    if "suppression_entries" in present:
        op.drop_table("suppression_entries")
