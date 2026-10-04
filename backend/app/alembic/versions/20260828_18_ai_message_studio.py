"""Production AI Message Studio: evolve ai_message_drafts.

Revision ID: 20260828_18
Revises: 20260828_17
"""

import json
from typing import Any

import sqlalchemy as sa
from alembic import op
from sqlalchemy import Column, inspect

revision = "20260828_18"
down_revision = "20260828_17"
branch_labels = None
depends_on = None

_RENAMES = {
    "status": "generation_status",
    "subject": "generated_subject",
    "html_body": "generated_body",
    "ai_provider": "provider",
    "ai_model": "model",
    "source_template_id": "template_id",
}


def _existing_columns(table: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def _table_names() -> set[str]:
    inspector = inspect(op.get_bind())
    return set(inspector.get_table_names())


def _constraints(table: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in inspect(op.get_bind()).get_foreign_keys(table)
    }


def _backfill_from_snapshots() -> None:
    connection = op.get_bind()
    columns = _existing_columns("ai_message_drafts")
    if "recipient_snapshot" not in columns or "sender_snapshot" not in columns:
        return
    rows = connection.execute(
        sa.text(
            "SELECT id, recipient_snapshot, sender_snapshot FROM ai_message_drafts"
        )
    ).fetchall()
    for row in rows:
        connection.execute(
            sa.text(
                "UPDATE ai_message_drafts SET input_context = :context "
                "WHERE id = :identifier"
            ),
            {
                "context": json.dumps(
                    {
                        "recipient": dict(row.recipient_snapshot or {}),
                        "sender": dict(row.sender_snapshot or {}),
                    }
                ),
                "identifier": row.id,
            },
        )


def upgrade() -> None:
    columns = _existing_columns("ai_message_drafts")

    for old, new in _RENAMES.items():
        if old in columns and new not in columns:
            op.alter_column(
                "ai_message_drafts", old, new_column_name=new
            )

    columns = _existing_columns("ai_message_drafts")
    if "input_context" not in columns:
        op.add_column(
            "ai_message_drafts",
            sa.Column(
                "input_context", sa.JSON(), nullable=False, server_default=sa.text("'{}'")
            ),
        )
    if "warnings" not in columns:
        op.add_column(
            "ai_message_drafts",
            sa.Column(
                "warnings", sa.JSON(), nullable=False, server_default=sa.text("'[]'")
            ),
        )

    if "input_context" in columns:
        _backfill_from_snapshots()

    defaulted = {
        "generation_type": (sa.String(length=40), "INITIAL_EMAIL"),
        "tone": (sa.String(length=40), "PROFESSIONAL"),
        "language": (sa.String(length=50), "English"),
        "desired_length": (sa.String(length=20), "MEDIUM"),
        "cta": (sa.String(length=2000), ""),
    }
    for name, (column_type, server_default) in defaulted.items():
        if name not in columns:
            op.add_column(
                "ai_message_drafts",
                sa.Column(name, column_type, nullable=False, server_default=server_default),
            )

    new_columns: dict[str, Column[Any]] = {
        "campaign_id": sa.Column("campaign_id", sa.Uuid(), nullable=True),
        "contact_id": sa.Column("contact_id", sa.Uuid(), nullable=True),
        "approved_by_id": sa.Column("approved_by_id", sa.Uuid(), nullable=True),
        "input_tokens": sa.Column("input_tokens", sa.Integer(), nullable=True),
        "output_tokens": sa.Column("output_tokens", sa.Integer(), nullable=True),
        "total_tokens": sa.Column("total_tokens", sa.Integer(), nullable=True),
        "estimated_cost": sa.Column("estimated_cost", sa.Numeric(18, 8), nullable=True),
        "cost_currency": sa.Column("cost_currency", sa.String(length=3), nullable=False, server_default="USD"),
        "request_duration_ms": sa.Column("request_duration_ms", sa.Integer(), nullable=True),
        "error_message": sa.Column("error_message", sa.String(length=1000), nullable=True),
        "approved_at": sa.Column("approved_at", sa.DateTime(timezone=True), nullable=True),
    }
    for name, column in new_columns.items():
        if name not in _existing_columns("ai_message_drafts"):
            op.add_column("ai_message_drafts", column)

    columns = _existing_columns("ai_message_drafts")
    constraints = _constraints("ai_message_drafts")
    if "campaign_id" in columns and "fk_ai_message_drafts_campaign_id_campaigns" not in constraints:
        op.create_foreign_key(
            "fk_ai_message_drafts_campaign_id_campaigns",
            "ai_message_drafts",
            "campaigns",
            ["campaign_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "contact_id" in columns and "fk_ai_message_drafts_contact_id_contacts" not in constraints:
        op.create_foreign_key(
            "fk_ai_message_drafts_contact_id_contacts",
            "ai_message_drafts",
            "contacts",
            ["contact_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "approved_by_id" in columns and "fk_ai_message_drafts_approved_by_id_users" not in constraints:
        op.create_foreign_key(
            "fk_ai_message_drafts_approved_by_id_users",
            "ai_message_drafts",
            "users",
            ["approved_by_id"],
            ["id"],
            ondelete="SET NULL",
        )

    connection = op.get_bind()
    connection.execute(
        sa.text(
            "UPDATE ai_message_drafts SET generation_status = 'REVIEW_REQUIRED' "
            "WHERE generation_status = 'IN_REVIEW'"
        )
    )
    connection.execute(
        sa.text(
            "UPDATE ai_message_drafts SET provider = 'MOCK_AI' WHERE provider IS NULL"
        )
    )

    for name in ("text_body", "recipient_snapshot", "sender_snapshot"):
        if name in _existing_columns("ai_message_drafts"):
            op.drop_column("ai_message_drafts", name)


def downgrade() -> None:
    columns = _existing_columns("ai_message_drafts")

    for column in ("campaign_id", "contact_id", "approved_by_id"):
        if column in columns:
            constraint = f"fk_ai_message_drafts_{column}_campaigns"
            if column == "contact_id":
                constraint = "fk_ai_message_drafts_contact_id_contacts"
            if column == "approved_by_id":
                constraint = "fk_ai_message_drafts_approved_by_id_users"
            op.drop_constraint(
                constraint, "ai_message_drafts", type_="foreignkey"
            )

    for column in (
        "input_context", "warnings", "generation_type", "tone", "language",
        "desired_length", "cta", "input_tokens", "output_tokens", "total_tokens",
        "estimated_cost", "cost_currency", "request_duration_ms", "error_message",
        "approved_at",
    ):
        if column in _existing_columns("ai_message_drafts"):
            op.drop_column("ai_message_drafts", column)

    for old, new in _RENAMES.items():  # reverse: new -> old
        if new in _existing_columns("ai_message_drafts"):
            op.alter_column("ai_message_drafts", new, new_column_name=old)

    op.add_column(
        "ai_message_drafts",
        sa.Column("text_body", sa.Text(), nullable=True),
    )
    op.add_column(
        "ai_message_drafts",
        sa.Column("recipient_snapshot", sa.JSON(), nullable=True),
    )
    op.add_column(
        "ai_message_drafts",
        sa.Column("sender_snapshot", sa.JSON(), nullable=True),
    )