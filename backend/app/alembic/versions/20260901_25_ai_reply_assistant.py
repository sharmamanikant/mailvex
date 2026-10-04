"""Phase 18 AI Reply Assistant: extend ai_reply_drafts with assistant metadata.

Revision ID: 20260901_25
Revises: 20260830_24
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260901_25"
down_revision = "20260830_24"
branch_labels = None
depends_on = None


def _columns() -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns("ai_reply_drafts")}


def upgrade() -> None:
    columns = _columns()
    additions = [
        ("operation", sa.String(length=30), "DRAFT"),
        ("intent", sa.String(length=40), None),
        ("intent_confidence", sa.Float(), None),
        ("warnings", sa.Text(), None),
        ("summary", sa.Text(), None),
        ("next_action", sa.Text(), None),
        ("tone", sa.String(length=40), None),
        ("language", sa.String(length=40), None),
        ("rejected_at", sa.DateTime(timezone=True), None),
    ]
    for name, column_type, server_default in additions:
        if name not in columns:
            op.add_column(
                "ai_reply_drafts",
                sa.Column(name, column_type, nullable=True, server_default=server_default),
            )

    if "source_draft_id" not in columns:
        op.add_column(
            "ai_reply_drafts",
            sa.Column("source_draft_id", sa.Uuid(), nullable=True),
        )
        op.create_foreign_key(
            "fk_ai_reply_drafts_source",
            "ai_reply_drafts",
            "ai_reply_drafts",
            ["source_draft_id"],
            ["id"],
            ondelete="SET NULL",
        )

    if "rejected_by_id" not in columns:
        op.add_column(
            "ai_reply_drafts",
            sa.Column("rejected_by_id", sa.Uuid(), nullable=True),
        )
        op.create_foreign_key(
            "fk_ai_reply_drafts_rejected_by",
            "ai_reply_drafts",
            "users",
            ["rejected_by_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    # Remove the FK constraints before the columns that reference them.
    op.drop_constraint("fk_ai_reply_drafts_source", "ai_reply_drafts", type_="foreignkey")
    op.drop_constraint("fk_ai_reply_drafts_rejected_by", "ai_reply_drafts", type_="foreignkey")
    for name in (
        "operation",
        "intent",
        "intent_confidence",
        "warnings",
        "summary",
        "next_action",
        "tone",
        "language",
        "source_draft_id",
        "rejected_by_id",
        "rejected_at",
    ):
        op.drop_column("ai_reply_drafts", name)
