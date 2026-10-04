"""Phase 7 Campaign Management: add campaign planning and targeting fields.

Revision ID: 20260828_19
Revises: 20260828_18
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260828_19"
down_revision = "20260828_18"
branch_labels = None
depends_on = None

_COLUMNS: list[tuple[str, sa.Column]] = [
    ("description", sa.Column("description", sa.String(length=2000), nullable=True)),
    (
        "recipient_list_id",
        sa.Column("recipient_list_id", sa.Uuid(), nullable=True),
    ),
    ("segment_id", sa.Column("segment_id", sa.Uuid(), nullable=True)),
    (
        "timezone",
        sa.Column("timezone", sa.String(length=100), server_default="UTC", nullable=False),
    ),
    ("scheduled_at", sa.Column("scheduled_at", sa.DateTime(timezone=True), nullable=True)),
]


def _columns(table: str) -> set[str]:
    return {column["name"] for column in inspect(op.get_bind()).get_columns(table)}


def _constraints(table: str) -> set[str]:
    return {
        constraint["name"]
        for constraint in inspect(op.get_bind()).get_foreign_keys(table)
    }


def upgrade() -> None:
    existing = _columns("campaigns")
    for name, column in _COLUMNS:
        if name not in existing:
            op.add_column("campaigns", column)
    constraints = _constraints("campaigns")
    if "recipient_list_id" in _columns("campaigns") and "fk_campaigns_recipient_list_id_contact_lists" not in constraints:
        op.create_foreign_key(
            "fk_campaigns_recipient_list_id_contact_lists",
            "campaigns",
            "contact_lists",
            ["recipient_list_id"],
            ["id"],
            ondelete="SET NULL",
        )
    if "segment_id" in _columns("campaigns") and "fk_campaigns_segment_id_contact_segments" not in constraints:
        op.create_foreign_key(
            "fk_campaigns_segment_id_contact_segments",
            "campaigns",
            "contact_segments",
            ["segment_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade() -> None:
    constraints = _constraints("campaigns")
    for name in (
        "fk_campaigns_segment_id_contact_segments",
        "fk_campaigns_recipient_list_id_contact_lists",
    ):
        if name in constraints:
            op.drop_constraint(name, "campaigns", type_="foreignkey")
    existing = _columns("campaigns")
    for name, _column in reversed(_COLUMNS):
        if name in existing:
            op.drop_column("campaigns", name)
