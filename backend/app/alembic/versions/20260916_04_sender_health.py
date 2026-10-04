"""Phase 5 Sender health engine storage.

Creates ``sender_health_checks`` (one row per evaluation run of a Sender) and
``sender_health_check_results`` (one row per check inside a run). Both are
tenant-scoped and attached to a ``Sender``; results cascade with their check.

Rows never store credentials, tokens, or raw DNS payloads: ``metadata`` is a
sanitized ``JSON`` blob in the same spirit as ``audit_logs.metadata``.

Revision ID: 20260916_04
Revises: 20260916_03
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260916_04"
down_revision = "20260916_03"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return bool(inspect(op.get_bind()).get_table_names().__contains__(name))


def _index_exists(name: str) -> bool:
    return bool(name in {ix["name"] for ix in inspect(op.get_bind()).get_indexes("sender_health_checks")})


def upgrade() -> None:
    if not _table_exists("senders"):
        raise RuntimeError("senders table missing; run 20260916_03 first")

    if not _table_exists("sender_health_checks"):
        op.create_table(
            "sender_health_checks",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Uuid(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "sender_id",
                sa.Uuid(),
                sa.ForeignKey("senders.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "overall_status",
                sa.String(length=30),
                nullable=False,
                server_default="CHECKING",
            ),
            sa.Column("overall_score", sa.Numeric(precision=5, scale=2), nullable=True),
            sa.Column("score_version", sa.String(length=20), nullable=False, server_default="v1"),
            sa.Column("triggered_by", sa.String(length=20), nullable=False, server_default="MANUAL"),
            sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("duration_ms", sa.Integer(), nullable=True),
            sa.Column("error_code", sa.String(length=50), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        )

    if _table_exists("sender_health_checks") and not _index_exists("ix_sender_health_checks_tenant_sender_created"):
        op.create_index(
            "ix_sender_health_checks_tenant_sender_created",
            "sender_health_checks",
            ["tenant_id", "sender_id", "created_at"],
        )

    if not _index_exists("ix_sender_health_checks_sender_created"):
        op.create_index(
            "ix_sender_health_checks_sender_created",
            "sender_health_checks",
            ["sender_id", "created_at"],
        )

    if not _table_exists("sender_health_check_results"):
        op.create_table(
            "sender_health_check_results",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Uuid(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "health_check_id",
                sa.Uuid(),
                sa.ForeignKey("sender_health_checks.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("check_type", sa.String(length=30), nullable=False),
            sa.Column("status", sa.String(length=20), nullable=False, server_default="UNKNOWN"),
            sa.Column("score", sa.Numeric(precision=5, scale=2), nullable=True),
            sa.Column("severity", sa.String(length=20), nullable=False, server_default="INFO"),
            sa.Column("title", sa.String(length=200), nullable=False),
            sa.Column("summary", sa.Text(), nullable=True),
            sa.Column("technical_details", sa.Text(), nullable=True),
            sa.Column("recommendation", sa.Text(), nullable=True),
            sa.Column("metadata", sa.JSON(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("checked_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
        )

    if _table_exists("sender_health_check_results"):
        results_table_ixs = {ix["name"] for ix in inspect(op.get_bind()).get_indexes("sender_health_check_results")}
        if "ix_sender_health_check_results_check" not in results_table_ixs:
            op.create_index(
                "ix_sender_health_check_results_check",
                "sender_health_check_results",
                ["health_check_id", "tenant_id"],
            )
        if "ix_sender_health_check_results_type_status" not in results_table_ixs:
            op.create_index(
                "ix_sender_health_check_results_type_status",
                "sender_health_check_results",
                ["tenant_id", "check_type", "status"],
            )


def downgrade() -> None:
    if _table_exists("sender_health_check_results"):
        op.drop_index("ix_sender_health_check_results_type_status", table_name="sender_health_check_results")
        op.drop_index("ix_sender_health_check_results_check", table_name="sender_health_check_results")
        op.drop_table("sender_health_check_results")
    if _table_exists("sender_health_checks"):
        op.drop_index("ix_sender_health_checks_sender_created", table_name="sender_health_checks")
        op.drop_index("ix_sender_health_checks_tenant_sender_created", table_name="sender_health_checks")
        op.drop_table("sender_health_checks")