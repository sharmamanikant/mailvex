"""Phase 21 observability + operations: alert records and ops metric samples.

Revision ID: 20260902_27
Revises: 20260901_26
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260902_27"
down_revision = "20260901_26"
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
    if not _table_exists("alert_records"):
        op.create_table(
            "alert_records",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Uuid(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("rule", sa.String(length=100), nullable=False),
            sa.Column("severity", sa.String(length=20), nullable=False, server_default="warning"),
            sa.Column("metric", sa.String(length=100), nullable=True),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="OPEN"),
            sa.Column("message", sa.String(length=1000), nullable=False),
            sa.Column("details", _json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("acknowledged_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "acknowledged_by",
                sa.Uuid(),
                sa.ForeignKey("users.id", ondelete="SET NULL"),
                nullable=True,
            ),
            sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        )
        op.create_index(
            "ix_alert_records_tenant_id",
            "alert_records",
            ["tenant_id"],
        )
        op.create_index(
            "ix_alert_records_tenant_status",
            "alert_records",
            ["tenant_id", "status"],
        )
        op.create_index(
            "ix_alert_records_tenant_created",
            "alert_records",
            ["tenant_id", "created_at"],
        )

    if not _table_exists("ops_metric_samples"):
        op.create_table(
            "ops_metric_samples",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Uuid(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=True,
            ),
            sa.Column("metric", sa.String(length=100), nullable=False),
            sa.Column("value", sa.Float(), nullable=False),
            sa.Column("labels", _json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("sampled_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        )
        op.create_index(
            "ix_ops_metric_samples_tenant_id",
            "ops_metric_samples",
            ["tenant_id"],
        )
        op.create_index(
            "ix_ops_metric_samples_tenant_metric_sampled",
            "ops_metric_samples",
            ["tenant_id", "metric", "sampled_at"],
        )


def downgrade() -> None:
    if _table_exists("ops_metric_samples"):
        op.drop_index("ix_ops_metric_samples_tenant_metric_sampled", table_name="ops_metric_samples")
        op.drop_index("ix_ops_metric_samples_tenant_id", table_name="ops_metric_samples")
        op.drop_table("ops_metric_samples")
    if _table_exists("alert_records"):
        op.drop_index("ix_alert_records_tenant_created", table_name="alert_records")
        op.drop_index("ix_alert_records_tenant_status", table_name="alert_records")
        op.drop_index("ix_alert_records_tenant_id", table_name="alert_records")
        op.drop_table("alert_records")