"""Phase 10Q sender engine (STEP 2): sender operational fields + warmup and
campaign-sender assignment tables.

Adds per-sender traffic enablement and daily ceilings, the per-sender warmup
configuration, the campaign-to-sender assignment join, and a
provider-metadata side table. All rows are tenant-scoped via the standard
``tenant_id`` FK pattern used across the schema. No secrets are stored here.

Revision ID: 20260909_01
Revises: 20260908_01
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260909_01"
down_revision = "20260908_01"
branch_labels = None
depends_on = None

SENDER_ACCOUNT_COLUMNS: list[tuple[str, sa.Column]] = [
    ("campaign_enabled", sa.Column("campaign_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False)),
    ("warmup_enabled", sa.Column("warmup_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False)),
    ("reply_sync_enabled", sa.Column("reply_sync_enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False)),
    ("daily_campaign_limit", sa.Column("daily_campaign_limit", sa.Integer(), server_default=sa.text("30"), nullable=False)),
    ("daily_warmup_limit", sa.Column("daily_warmup_limit", sa.Integer(), server_default=sa.text("20"), nullable=False)),
    ("last_success_at", sa.Column("last_success_at", sa.DateTime(timezone=True), nullable=True)),
    ("last_failure_at", sa.Column("last_failure_at", sa.DateTime(timezone=True), nullable=True)),
    ("last_error", sa.Column("last_error", sa.Text(), nullable=True)),
]


def _table_exists(name: str) -> bool:
    return bool(inspect(op.get_bind()).get_table_names().__contains__(name))


def _columns(table: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def _json_type():
    if op.get_bind().dialect.name == "postgresql":
        import sqlalchemy.dialects.postgresql

        return sqlalchemy.dialects.postgresql.JSONB()
    return sa.JSON()


def upgrade() -> None:
    if _table_exists("sender_accounts"):
        existing = _columns("sender_accounts")
        for name, column in SENDER_ACCOUNT_COLUMNS:
            if name not in existing:
                op.add_column("sender_accounts", column)

    if not _table_exists("sender_provider_metadata"):
        op.create_table(
            "sender_provider_metadata",
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
                sa.ForeignKey("sender_accounts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("provider", sa.String(length=30), nullable=False),
            sa.Column("metadata", _json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.UniqueConstraint("tenant_id", "sender_id", "provider", name="uq_sender_provider_metadata_tenant_sender_provider"),
        )
        op.create_index(
            "ix_sender_provider_metadata_tenant_provider",
            "sender_provider_metadata",
            ["tenant_id", "provider"],
        )

    if not _table_exists("warmup_settings"):
        op.create_table(
            "warmup_settings",
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
                sa.ForeignKey("sender_accounts.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column("enabled", sa.Boolean(), server_default=sa.text("false"), nullable=False),
            sa.Column("daily_limit", sa.Integer(), server_default=sa.text("20"), nullable=False),
            sa.Column("reply_rate_target", sa.Float(), server_default=sa.text("0.5"), nullable=False),
            sa.Column("start_time", sa.String(length=5), nullable=True),
            sa.Column("end_time", sa.String(length=5), nullable=True),
            sa.Column("weekdays", _json_type(), nullable=False, server_default=sa.text("'[]'")),
            sa.Column("minimum_delay", sa.Integer(), server_default=sa.text("60"), nullable=False),
            sa.Column("maximum_delay", sa.Integer(), server_default=sa.text("3600"), nullable=False),
            sa.Column("target_provider_distribution", _json_type(), nullable=False, server_default=sa.text("'{}'")),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.UniqueConstraint("tenant_id", "sender_id", name="uq_warmup_settings_tenant_sender"),
        )
        op.create_index(
            "ix_warmup_settings_sender_id",
            "warmup_settings",
            ["sender_id"],
        )

    if not _table_exists("campaign_senders"):
        op.create_table(
            "campaign_senders",
            sa.Column("id", sa.Uuid(), primary_key=True),
            sa.Column(
                "tenant_id",
                sa.Uuid(),
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "campaign_id",
                sa.Uuid(),
                sa.ForeignKey("campaigns.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "sender_id",
                sa.Uuid(),
                sa.ForeignKey("sender_accounts.id", ondelete="RESTRICT"),
                nullable=False,
            ),
            sa.Column("daily_limit", sa.Integer(), nullable=True),
            sa.Column("enabled", sa.Boolean(), server_default=sa.text("true"), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("CURRENT_TIMESTAMP"), nullable=False),
            sa.UniqueConstraint("tenant_id", "campaign_id", "sender_id", name="uq_campaign_sender"),
        )
        op.create_index(
            "ix_campaign_senders_tenant_sender",
            "campaign_senders",
            ["tenant_id", "sender_id"],
        )


def downgrade() -> None:
    if _table_exists("campaign_senders"):
        op.drop_index("ix_campaign_senders_tenant_sender", table_name="campaign_senders")
        op.drop_table("campaign_senders")
    if _table_exists("warmup_settings"):
        op.drop_index("ix_warmup_settings_sender_id", table_name="warmup_settings")
        op.drop_table("warmup_settings")
    if _table_exists("sender_provider_metadata"):
        op.drop_index("ix_sender_provider_metadata_tenant_provider", table_name="sender_provider_metadata")
        op.drop_table("sender_provider_metadata")
    if _table_exists("sender_accounts"):
        existing = _columns("sender_accounts")
        for name, _column in reversed(SENDER_ACCOUNT_COLUMNS):
            if name in existing:
                op.drop_column("sender_accounts", name)