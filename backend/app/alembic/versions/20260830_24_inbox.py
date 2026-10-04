"""Phase 17 Unified Inbox: email_threads, email_messages, ai_reply_drafts.

Revision ID: 20260830_24
Revises: 20260830_23
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260830_24"
down_revision = "20260830_23"
branch_labels = None
depends_on = None


def _tables() -> set[str]:
    return {table for table in inspect(op.get_bind()).get_table_names()}


def upgrade() -> None:
    if "email_threads" not in _tables():
        op.create_table(
            "email_threads",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("sender_id", sa.Uuid(), nullable=False),
            sa.Column("external_thread_id", sa.String(length=500), nullable=False),
            sa.Column("subject", sa.String(length=998), nullable=True),
            sa.Column("last_message_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("status", sa.String(length=30), server_default="UNREAD", nullable=False),
            sa.Column("contact_id", sa.Uuid(), nullable=True),
            sa.Column("campaign_id", sa.Uuid(), nullable=True),
            sa.Column("match_status", sa.String(length=30), server_default="UNMATCHED", nullable=False),
            sa.Column("assigned_user_id", sa.Uuid(), nullable=True),
            sa.Column("provider", sa.String(length=30), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["sender_id"], ["email_accounts.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["contact_id"], ["contacts.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["campaign_id"], ["campaigns.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["assigned_user_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id", name="pk_email_threads"),
            sa.UniqueConstraint("tenant_id", "sender_id", "external_thread_id", name="uq_email_threads_provider"),
        )
        op.create_index("ix_email_threads_tenant_status", "email_threads", ["tenant_id", "status"])

    if "email_messages" not in _tables():
        op.create_table(
            "email_messages",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("thread_id", sa.Uuid(), nullable=False),
            sa.Column("external_message_id", sa.String(length=500), nullable=False),
            sa.Column("direction", sa.String(length=20), nullable=False),
            sa.Column("from_email", sa.String(length=320), nullable=False),
            sa.Column("to_email", sa.String(length=320), nullable=False),
            sa.Column("subject", sa.String(length=998), nullable=False),
            sa.Column("body_reference", sa.String(length=500), nullable=True),
            sa.Column("body_text", sa.Text(), nullable=True),
            sa.Column("body_html", sa.Text(), nullable=True),
            sa.Column("received_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("provider", sa.String(length=30), nullable=False),
            sa.Column("status", sa.String(length=30), server_default="RECEIVED", nullable=False),
            sa.Column("in_reply_to", sa.String(length=500), nullable=True),
            sa.Column("references", sa.Text(), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["thread_id"], ["email_threads.id"], ondelete="CASCADE"),
            sa.PrimaryKeyConstraint("id", name="pk_email_messages"),
            sa.UniqueConstraint("tenant_id", "external_message_id", name="uq_email_messages_provider"),
        )
        op.create_index("ix_email_messages_tenant_thread", "email_messages", ["tenant_id", "thread_id"])

    if "ai_reply_drafts" not in _tables():
        op.create_table(
            "ai_reply_drafts",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("thread_id", sa.Uuid(), nullable=False),
            sa.Column("created_by_id", sa.Uuid(), nullable=True),
            sa.Column("subject", sa.String(length=998), nullable=False),
            sa.Column("body", sa.Text(), nullable=False),
            sa.Column("status", sa.String(length=30), server_default="DRAFT", nullable=False),
            sa.Column("provider", sa.String(length=30), server_default="MOCK", nullable=False),
            sa.Column("model", sa.String(length=120), nullable=True),
            sa.Column("input_tokens", sa.Integer(), nullable=True),
            sa.Column("output_tokens", sa.Integer(), nullable=True),
            sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.text("now()"), nullable=False),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["thread_id"], ["email_threads.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id", name="pk_ai_reply_drafts"),
        )
        op.create_index("ix_ai_reply_drafts_tenant_thread", "ai_reply_drafts", ["tenant_id", "thread_id"])


def downgrade() -> None:
    tables = _tables()
    for name in ("ai_reply_drafts", "email_messages", "email_threads"):
        if name in tables:
            op.drop_table(name)
