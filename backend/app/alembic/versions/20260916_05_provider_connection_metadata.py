"""Phase 6 Microsoft 365 org-connection metadata.

Extends ``provider_connections`` with:

* ``metadata`` (JSON) — provider-authored, non-secret connection metadata such
  as the Microsoft 365 organization name, default verified domain and tenant
  id (``microsoftTenantId``). Mirrors ``sender_connections.metadata``.
* ``last_sync_stats`` (JSON) — summary counts from the most recent mailbox
  sync run (created / updated / suspended / soft_deleted / skipped / errors).

    * No Microsoft-specific tables are introduced: Microsoft 365 mailboxes are
      discovered into the existing ``mailboxes`` rows and promoted to
      ``senders`` exactly like Google Workspace ones.
    * Duplicate prevention for a Microsoft tenant is covered by the existing
      unique ``(tenant_id, provider, provider_account_id)`` index from
      ``20260916_01`` because the Microsoft connection's ``provider_account_id``
      is the Microsoft tenant id.

Revision ID: 20260916_05
Revises: 20260916_04
"""

import sqlalchemy as sa
from alembic import op
from sqlalchemy import inspect

revision = "20260916_05"
down_revision = "20260916_04"
branch_labels = None
depends_on = None


def _table_exists(name: str) -> bool:
    return bool(inspect(op.get_bind()).get_table_names().__contains__(name))


def _column_exists(table: str, column: str) -> bool:
    return bool(column in {col["name"] for col in inspect(op.get_bind()).get_columns(table)})


def _json_type():
    if op.get_bind().dialect.name == "postgresql":
        import sqlalchemy.dialects.postgresql

        return sqlalchemy.dialects.postgresql.JSONB()
    return sa.JSON()


def upgrade() -> None:
    if not _table_exists("provider_connections"):
        raise RuntimeError("provider_connections table missing; run 20260916_01 first")
    if not _column_exists("provider_connections", "metadata"):
        op.add_column(
            "provider_connections",
            sa.Column("metadata", _json_type(), nullable=False, server_default=sa.text("'{}'")),
        )
    if not _column_exists("provider_connections", "last_sync_stats"):
        op.add_column(
            "provider_connections",
            sa.Column("last_sync_stats", _json_type(), nullable=False, server_default=sa.text("'{}'")),
        )


def downgrade() -> None:
    if _column_exists("provider_connections", "last_sync_stats"):
        op.drop_column("provider_connections", "last_sync_stats")
    if _column_exists("provider_connections", "metadata"):
        op.drop_column("provider_connections", "metadata")