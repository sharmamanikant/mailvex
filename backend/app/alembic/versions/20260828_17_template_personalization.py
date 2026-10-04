"""Template personalization and AI message draft architecture.

Revision ID: 20260828_17
Revises: 20260828_16
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260828_17"
down_revision = "20260828_16"
branch_labels = None
depends_on = None


def _existing_columns(table: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def _table_names() -> set[str]:
    inspector = inspect(op.get_bind())
    return set(inspector.get_table_names())


def upgrade() -> None:
    templates = _existing_columns("templates")
    if "created_by_id" not in templates:
        op.add_column(
            "templates",
            sa.Column("created_by_id", sa.Uuid(), nullable=True),
        )
        op.create_foreign_key(
            "fk_templates_created_by_id_users",
            "templates",
            "users",
            ["created_by_id"],
            ["id"],
            ondelete="SET NULL",
        )

    versions = _existing_columns("template_versions")
    if "created_by_id" not in versions:
        op.add_column(
            "template_versions",
            sa.Column("created_by_id", sa.Uuid(), nullable=True),
        )
        op.create_foreign_key(
            "fk_template_versions_created_by_id_users",
            "template_versions",
            "users",
            ["created_by_id"],
            ["id"],
            ondelete="SET NULL",
        )

    tables = _table_names()
    if "ai_message_drafts" not in tables:
        op.create_table(
            "ai_message_drafts",
            sa.Column("id", sa.Uuid(), nullable=False),
            sa.Column("tenant_id", sa.Uuid(), nullable=False),
            sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("updated_at", sa.DateTime(timezone=True), server_default=sa.func.now(), nullable=False),
            sa.Column("created_by_id", sa.Uuid(), nullable=True),
            sa.Column("reviewed_by_id", sa.Uuid(), nullable=True),
            sa.Column("source_template_id", sa.Uuid(), nullable=True),
            sa.Column("approved_template_id", sa.Uuid(), nullable=True),
            sa.Column("objective", sa.Text(), nullable=False),
            sa.Column("subject", sa.String(length=998), nullable=False),
            sa.Column("html_body", sa.Text(), nullable=False),
            sa.Column("text_body", sa.Text(), nullable=True),
            sa.Column("recipient_snapshot", sa.JSON(), nullable=False),
            sa.Column("sender_snapshot", sa.JSON(), nullable=False),
            sa.Column("status", sa.String(length=30), server_default="DRAFT", nullable=False),
            sa.Column("generation_method", sa.String(length=30), server_default="MOCK_AI", nullable=False),
            sa.Column("ai_provider", sa.String(length=100), nullable=True),
            sa.Column("ai_model", sa.String(length=100), nullable=True),
            sa.Column("review_note", sa.String(length=1000), nullable=True),
            sa.Column("reviewed_at", sa.DateTime(timezone=True), nullable=True),
            sa.ForeignKeyConstraint(["tenant_id"], ["tenants.id"], ondelete="CASCADE"),
            sa.ForeignKeyConstraint(["created_by_id"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["reviewed_by_id"], ["users.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["source_template_id"], ["templates.id"], ondelete="SET NULL"),
            sa.ForeignKeyConstraint(["approved_template_id"], ["templates.id"], ondelete="SET NULL"),
            sa.PrimaryKeyConstraint("id"),
        )
        op.create_index(
            "ix_ai_message_drafts_tenant_created",
            "ai_message_drafts",
            ["tenant_id", "created_at"],
        )
        op.create_index(
            "ix_ai_message_drafts_tenant_status",
            "ai_message_drafts",
            ["tenant_id", "status"],
        )


def downgrade() -> None:
    tables = _table_names()
    if "ai_message_drafts" in tables:
        op.drop_table("ai_message_drafts")

    versions = _existing_columns("template_versions")
    if "created_by_id" in versions:
        op.drop_constraint(
            "fk_template_versions_created_by_id_users",
            "template_versions",
            type_="foreignkey",
        )
        op.drop_column("template_versions", "created_by_id")

    templates = _existing_columns("templates")
    if "created_by_id" in templates:
        op.drop_constraint(
            "fk_templates_created_by_id_users",
            "templates",
            type_="foreignkey",
        )
        op.drop_column("templates", "created_by_id")