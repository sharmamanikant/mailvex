"""Phase 20 SaaS usage + billing foundation: immutable usage events and
tenant subscriptions.

Revision ID: 20260901_26
Revises: 20260901_25
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260901_26"
down_revision = "20260901_25"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return bool(inspect(op.get_bind()).get_table_names().__contains__(name))


def _json_type():
    if op.get_bind().dialect.name == "postgresql":
        import sqlalchemy.dialects.postgresql

        return sqlalchemy.dialects.postgresql.JSONB()
    return sa.JSON()


def upgrade() -> None:
    if not _table_exists("tenant_subscriptions"):
        op.create_table(
            "tenant_subscriptions",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Uuid(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("plan_code", sa.String(length=30), nullable=False, server_default="free"),
            sa.Column("status", sa.String(length=30), nullable=False, server_default="ACTIVE"),
            sa.Column("seats", sa.Integer(), nullable=True),
            sa.Column("custom_limits", _json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("period_start", sa.DateTime(timezone=True), nullable=True),
            sa.Column("period_end", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.UniqueConstraint("tenant_id", name="uq_tenant_subscriptions_tenant"),
        )
        op.create_index(
            "ix_tenant_subscriptions_tenant_id",
            "tenant_subscriptions",
            ["tenant_id"],
        )

    if not _table_exists("usage_events"):
        op.create_table(
            "usage_events",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Uuid(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "actor_id",
                sa.Uuid(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("event_type", sa.String(length=50), nullable=False),
            sa.Column("period_start", sa.DateTime(timezone=True), nullable=False),
            sa.Column("quantity", sa.Numeric(precision=18, scale=4), nullable=False, server_default=sa.text("1")),
            sa.Column("resource_type", sa.String(length=50), nullable=True),
            sa.Column("resource_id", sa.Uuid(), nullable=True),
            sa.Column("metadata", _json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        )
        op.create_index(
            "ix_usage_events_tenant_id",
            "usage_events",
            ["tenant_id"],
        )
        op.create_index(
            "ix_usage_events_tenant_period_type",
            "usage_events",
            ["tenant_id", "period_start", "event_type"],
        )


def downgrade() -> None:
    if _table_exists("usage_events"):
        op.drop_index("ix_usage_events_tenant_period_type", table_name="usage_events")
        op.drop_index("ix_usage_events_tenant_id", table_name="usage_events")
        op.drop_table("usage_events")
    if _table_exists("tenant_subscriptions"):
        op.drop_index("ix_tenant_subscriptions_tenant_id", table_name="tenant_subscriptions")
        op.drop_table("tenant_subscriptions")