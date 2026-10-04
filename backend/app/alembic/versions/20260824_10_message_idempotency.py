"""Protect one outbound message per scheduled message.

Revision ID: 20260824_10
Revises: 20260824_09
"""

from alembic import op
from sqlalchemy import inspect

revision = "20260824_10"
down_revision = "20260824_09"
branch_labels = None
depends_on = None


def upgrade() -> None:
    constraints = {
        item.get("name")
        for item in inspect(op.get_bind()).get_unique_constraints("messages")
    }
    if "uq_messages_scheduled_message" not in constraints:
        op.create_unique_constraint(
            "uq_messages_scheduled_message",
            "messages",
            ["tenant_id", "scheduled_message_id"],
        )


def downgrade() -> None:
    constraints = {
        item.get("name")
        for item in inspect(op.get_bind()).get_unique_constraints("messages")
    }
    if "uq_messages_scheduled_message" in constraints:
        with op.batch_alter_table("messages") as batch:
            batch.drop_constraint("uq_messages_scheduled_message", type_="unique")
