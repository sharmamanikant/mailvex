"""Phase 10C: enforce one Microsoft 365 connection per external account (tenant-scoped).

Spec item 13 (duplicate protection): a verified Microsoft account may drive
only one sender connection per tenant. The partial unique index allows
nullable ``external_account_id`` rows (connections that have not been
authorized yet) while guaranteeing uniqueness over ``(tenant_id, provider,
external_account_id)`` for MICROSOFT connections once the identity is known.
Preserves existing Google connections.

Revision ID: 20260908_01
Revises: 20260906_01
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260908_01"
down_revision = "20260906_01"
branch_labels = None
depends_on = None

INDEX_NAME = "uq_sender_connections_microsoft_account"
WHERE_CLAUSE = "provider = 'MICROSOFT' AND external_account_id IS NOT NULL"


def _table_exists(name: str) -> bool:
    return bool(inspect(op.get_bind()).get_table_names().__contains__(name))


def upgrade() -> None:
    if _table_exists("sender_connections"):
        op.create_index(
            INDEX_NAME,
            "sender_connections",
            ["tenant_id", "provider", "external_account_id"],
            unique=True,
            sqlite_where=sa.text(WHERE_CLAUSE),
            postgresql_where=sa.text(WHERE_CLAUSE),
        )


def downgrade() -> None:
    if _table_exists("sender_connections"):
        op.drop_index(INDEX_NAME, table_name="sender_connections")
