"""Add unified conversation metadata.

Revision ID: 20260824_11
Revises: 20260824_10
"""

from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260824_11"
down_revision = "20260824_10"
branch_labels = None
depends_on = None


def upgrade() -> None:
    bind = op.get_bind()
    thread_columns = {column["name"] for column in inspect(bind).get_columns("threads")}
    thread_additions: list[tuple[str, Any]] = [
        ("subject", sa.Column("subject", sa.String(length=998), nullable=True)),
        ("assigned_user_id", sa.Column("assigned_user_id", sa.Uuid(), nullable=True)),
        ("notes", sa.Column("notes", sa.Text(), nullable=True)),
        ("tags", sa.Column("tags", sa.JSON(), server_default="[]", nullable=False)),
        ("last_message_at", sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True)),
    ]
    with op.batch_alter_table("threads") as batch:
        for name, column in thread_additions:
            if name not in thread_columns:
                batch.add_column(column)
        if "assigned_user_id" not in thread_columns:
            batch.create_foreign_key("fk_threads_assigned_user_id_users", "users", ["assigned_user_id"], ["id"], ondelete="SET NULL")
    reply_columns = {column["name"] for column in inspect(bind).get_columns("replies")}
    reply_additions: list[tuple[str, Any]] = [
        ("message_id", sa.Column("message_id", sa.Uuid(), nullable=True)),
        ("sender_email", sa.Column("sender_email", sa.String(length=320), nullable=True)),
        ("recipient_email", sa.Column("recipient_email", sa.String(length=320), nullable=True)),
        ("body_html", sa.Column("body_html", sa.Text(), nullable=True)),
    ]
    with op.batch_alter_table("replies") as batch:
        for name, column in reply_additions:
            if name not in reply_columns:
                batch.add_column(column)
        if "message_id" not in reply_columns:
            batch.create_foreign_key("fk_replies_message_id_messages", "messages", ["message_id"], ["id"], ondelete="SET NULL")


def downgrade() -> None:
    with op.batch_alter_table("replies") as batch:
        for name in ("body_html", "recipient_email", "sender_email", "message_id"):
            batch.drop_column(name)
    with op.batch_alter_table("threads") as batch:
        for name in ("last_message_at", "tags", "notes", "assigned_user_id", "subject"):
            batch.drop_column(name)