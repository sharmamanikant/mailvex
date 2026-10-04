"""Phase 10Q sender engine (STEP 3): route delivery jobs through System B senders.

Adds a nullable ``sender_account_id`` on ``delivery_jobs`` pointing at
``sender_accounts`` (System B). When materializing a campaign that has a sender
pool, each job is assigned a System B sender from that pool (the existing
non-nullable ``sender_id`` stays for legacy Single-System-A-sender campaigns).

Revision ID: 20260909_02
Revises: 20260909_01
"""

import sqlalchemy as sa
from alembic import op

revision = "20260909_02"
down_revision = "20260909_01"
branch_labels = None
depends_on = None


def _table_exists(bind, name: str) -> bool:
    from sqlalchemy import inspect

    return name in set(inspect(bind).get_table_names())


def upgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "delivery_jobs"):
        return
    columns = {column["name"] for column in bind.dialect.get_columns(bind, "delivery_jobs")}
    if "sender_account_id" in columns:
        return
    op.add_column(
        "delivery_jobs",
        sa.Column(
            "sender_account_id",
            sa.Uuid(),
            sa.ForeignKey("sender_accounts.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    )
    op.create_index(
        "ix_delivery_jobs_sender_account_id",
        "delivery_jobs",
        ["sender_account_id"],
    )


def downgrade() -> None:
    bind = op.get_bind()
    if not _table_exists(bind, "delivery_jobs"):
        return
    columns = {column["name"] for column in bind.dialect.get_columns(bind, "delivery_jobs")}
    if "sender_account_id" not in columns:
        return
    op.drop_index("ix_delivery_jobs_sender_account_id", table_name="delivery_jobs")
    op.drop_column("delivery_jobs", "sender_account_id")