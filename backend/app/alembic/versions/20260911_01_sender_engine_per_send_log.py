"""Sender engine (STEP 2): enrich the durable per-send log.

``messages`` gains the System B ``sender_account_id`` link (parallel to the
existing legacy ``sender_id``), traffic class (``msg_type``), recipient,
the RFC 5322 ``message_id`` header, and per-stage status timestamps plus
``error_code``/``error_message``. ``message_events`` gains a nullable
``sender_account_id`` link and a tenant/sender index.

OAuth connect-state for the sender flows is intentionally NOT given its own
table: the Redis-backed :class:`OAuthStateStore` already provides the required
single-use/tenant-bound/short-lived semantics with atomic ``GETDEL``.

No secrets are stored here. All rows are tenant-scoped via the standard
``tenant_id`` FK pattern.

Revision ID: 20260911_01
Revises: 20260909_02
"""

import sqlalchemy as sa
from alembic import op

revision = "20260911_01"
down_revision = "20260909_02"
branch_labels = None
depends_on = None


def _has_table(bind, name: str) -> bool:
    from sqlalchemy import inspect

    return name in set(inspect(bind).get_table_names())


def _columns(bind, table: str) -> set[str]:
    return {column["name"] for column in bind.dialect.get_columns(bind, table)}


def _index_names(bind, table: str) -> set[str]:
    return {index["name"] for index in bind.dialect.get_indexes(bind, table)}


MESSAGE_COLUMNS: list[tuple[str, sa.Column]] = [
    (
        "sender_account_id",
        sa.Column(
            "sender_account_id",
            sa.Uuid(),
            sa.ForeignKey("sender_accounts.id", ondelete="RESTRICT"),
            nullable=True,
        ),
    ),
    (
        "msg_type",
        sa.Column(
            "msg_type",
            sa.String(length=20),
            server_default=sa.text("'CAMPAIGN'"),
            nullable=False,
        ),
    ),
    ("recipient", sa.Column("recipient", sa.String(length=320), nullable=True)),
    ("message_id", sa.Column("message_id", sa.String(length=998), nullable=True)),
    ("queued_at", sa.Column("queued_at", sa.DateTime(timezone=True), nullable=True)),
    ("sent_at", sa.Column("sent_at", sa.DateTime(timezone=True), nullable=True)),
    ("delivered_at", sa.Column("delivered_at", sa.DateTime(timezone=True), nullable=True)),
    ("failed_at", sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True)),
    ("error_code", sa.Column("error_code", sa.String(length=100), nullable=True)),
    ("error_message", sa.Column("error_message", sa.Text(), nullable=True)),
]


def upgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "messages"):
        existing = _columns(bind, "messages")
        for name, column in MESSAGE_COLUMNS:
            if name not in existing:
                op.add_column("messages", column)
        if "sender_account_id" in _columns(bind, "messages"):
            if "ix_messages_tenant_sender_account" not in _index_names(bind, "messages"):
                op.create_index(
                    "ix_messages_tenant_sender_account",
                    "messages",
                    ["tenant_id", "sender_account_id"],
                )
        if "ix_messages_message_id" not in _index_names(bind, "messages"):
            op.create_index("ix_messages_message_id", "messages", ["message_id"])

    if _has_table(bind, "message_events"):
        if "sender_account_id" not in _columns(bind, "message_events"):
            op.add_column(
                "message_events",
                sa.Column(
                    "sender_account_id",
                    sa.Uuid(),
                    sa.ForeignKey("sender_accounts.id", ondelete="RESTRICT"),
                    nullable=True,
                ),
            )
        if "ix_message_events_tenant_sender" not in _index_names(bind, "message_events"):
            op.create_index(
                "ix_message_events_tenant_sender",
                "message_events",
                ["tenant_id", "sender_account_id"],
            )


def downgrade() -> None:
    bind = op.get_bind()

    if _has_table(bind, "message_events"):
        for index in ("ix_message_events_tenant_sender",):
            if index in _index_names(bind, "message_events"):
                op.drop_index(index, table_name="message_events")
        if "sender_account_id" in _columns(bind, "message_events"):
            op.drop_column("message_events", "sender_account_id")

    if _has_table(bind, "messages"):
        for index in ("ix_messages_message_id", "ix_messages_tenant_sender_account"):
            if index in _index_names(bind, "messages"):
                op.drop_index(index, table_name="messages")
        existing = _columns(bind, "messages")
        for name, _ in reversed(MESSAGE_COLUMNS):
            if name in existing:
                op.drop_column("messages", name)