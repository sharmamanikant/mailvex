"""Contact validation engine: verdict columns, verification jobs, domain rules.

Chained to verified head ``20260916_07`` (sender_pools).

Design contract:

* The existing ``contacts`` table gains **verdict columns only** - no parallel
  contact system. ``contacts.validation_status`` is deliberately NOT reused: it
  is campaign-exclusion semantics (``services/campaigns.py`` drops INVALID /
  DISPOSABLE / ROLE_ACCOUNT recipients, and ``services/compliance.py`` enforces
  the same rule). Overwriting it from a scoring pass would silently remove
  genuine contacts from sends, so the engine writes to its own columns.
* Every verdict column is nullable or server-defaulted, so the 199 pre-existing
  contacts and every other existing row migrate without a backfill.
* ``verification_jobs`` carries the bulk counters the CRM UI renders
  (total / queued / processing / completed / failed / valid / invalid / risky /
  needs-review / duplicates) without loading contacts into memory.
* ``validation_domain_rules`` is the configurable disposable / free-provider /
  role-account reference set. It is intentionally **global, not tenant-owned**:
  these are universal facts about public DNS, identical for every tenant, and
  duplicating them per tenant would be a correctness hazard. Tenant scoping is
  mandatory on all *results* (contacts, verification_jobs), not on the reference
  table.

Revision ID: 20260925_08
Revises: 20260916_07
Create Date: 2026-09-25
"""

from __future__ import annotations

from collections.abc import Sequence

import sqlalchemy as sa
from alembic import op
from sqlalchemy.dialects import postgresql

revision: str = "20260925_08"
down_revision: str | None = "20260916_07"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None

JSONType = sa.JSON().with_variant(postgresql.JSONB(), "postgresql")
UUID = postgresql.UUID(as_uuid=True)

_CONTACT_VERDICT_COLUMNS: tuple[tuple[str, sa.types.TypeEngine, sa.TextClause | None], ...] = (
    ("email_status", sa.String(30), sa.text("'UNKNOWN'")),
    ("email_type", sa.String(30), sa.text("'UNKNOWN'")),
    ("email_provider", sa.String(60), sa.text("'UNKNOWN'")),
    ("domain_status", sa.String(30), sa.text("'UNKNOWN'")),
    ("mx_status", sa.String(30), sa.text("'UNKNOWN'")),
    ("smtp_status", sa.String(30), sa.text("'NOT_PROBED'")),
    ("disposable", sa.Boolean(), sa.text("false")),
    ("role_account", sa.Boolean(), sa.text("false")),
    ("catch_all", sa.Boolean(), None),
    ("phone_status", sa.String(30), sa.text("'UNKNOWN'")),
    ("phone_type", sa.String(30), sa.text("'UNKNOWN'")),
    ("company_status", sa.String(30), sa.text("'UNKNOWN'")),
    ("duplicate_status", sa.String(30), sa.text("'UNIQUE'")),
    ("duplicate_score", sa.Integer(), None),
    ("verification_score", sa.Integer(), None),
    ("risk_level", sa.String(20), sa.text("'UNKNOWN'")),
    ("verification_status", sa.String(30), sa.text("'UNKNOWN'")),
    ("last_verified_at", sa.DateTime(timezone=True), None),
    ("verification_details", JSONType, None),
)


def _table_exists(name: str) -> bool:
    return sa.inspect(op.get_bind()).has_table(name)


def _column_exists(table: str, name: str) -> bool:
    return any(col["name"] == name for col in sa.inspect(op.get_bind()).get_columns(table))


def _index_exists(name: str) -> bool:
    inspector = sa.inspect(op.get_bind())
    return any(
        name in {idx["name"] for idx in inspector.get_indexes(table)}
        for table in inspector.get_table_names()
    )


def upgrade() -> None:
    for name, column_type, default in _CONTACT_VERDICT_COLUMNS:
        if _column_exists("contacts", name):
            continue
        # Columns with a constant server default are added NOT NULL: PostgreSQL
        # backfills every existing row from the default in a single table
        # rewrite, so the result matches the model's ``nullable=False``
        # declaration. Signal columns whose honest initial value is "unknown"
        # (catch_all, scores, timestamps, details) stay nullable.
        #
        # The default MUST be passed to the constructor: alembic's add_column
        # silently drops a server_default assigned to the Column afterwards,
        # which would then fail the NOT NULL check on every pre-existing row.
        column = sa.Column(
            name, column_type, nullable=default is None, server_default=default
        )
        op.add_column("contacts", column)

    for name, columns in (
        ("ix_contacts_tenant_verification_status", ["tenant_id", "verification_status"]),
        ("ix_contacts_tenant_verification_score", ["tenant_id", "verification_score"]),
        ("ix_contacts_tenant_risk_level", ["tenant_id", "risk_level"]),
        ("ix_contacts_tenant_last_verified", ["tenant_id", "last_verified_at"]),
    ):
        if not _index_exists(name):
            op.create_index(name, "contacts", columns)

    if not _table_exists("verification_jobs"):
        op.create_table(
            "verification_jobs",
            sa.Column("id", UUID, primary_key=True),
            sa.Column(
                "tenant_id",
                UUID,
                sa.ForeignKey("tenants.id", ondelete="CASCADE"),
                nullable=False,
            ),
            sa.Column(
                "created_by_id", UUID, sa.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
            ),
            sa.Column("status", sa.String(30), nullable=False, server_default="QUEUED"),
            sa.Column("scope", sa.String(20), nullable=False, server_default="BULK"),
            sa.Column("total_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("processed_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("valid_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("invalid_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("risky_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("needs_review_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("duplicate_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("unknown_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column("failed_count", sa.Integer(), nullable=False, server_default="0"),
            sa.Column(
                "request_payload", JSONType, nullable=False, server_default=sa.text("'{}'")
            ),
            sa.Column("celery_task_id", sa.String(100), nullable=True),
            sa.Column("error_message", sa.Text(), nullable=True),
            sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
        )

    for name, columns in (
        ("ix_verification_jobs_tenant_status", ["tenant_id", "status"]),
        ("ix_verification_jobs_tenant_created", ["tenant_id", "created_at"]),
    ):
        if not _index_exists(name):
            op.create_index(name, "verification_jobs", columns)

    if not _table_exists("validation_domain_rules"):
        op.create_table(
            "validation_domain_rules",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("domain", sa.String(255), nullable=False),
            sa.Column("category", sa.String(30), nullable=False),
            sa.Column("provider", sa.String(60), nullable=True),
            sa.Column("source", sa.String(60), nullable=False, server_default="seed"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "domain", "category", name="uq_validation_domain_rules_domain_category"
            ),
        )

    if not _index_exists("ix_validation_domain_rules_domain"):
        op.create_index("ix_validation_domain_rules_domain", "validation_domain_rules", ["domain"])

    if not _table_exists("validation_local_rules"):
        op.create_table(
            "validation_local_rules",
            sa.Column("id", UUID, primary_key=True),
            sa.Column("local_part", sa.String(255), nullable=False),
            sa.Column("category", sa.String(30), nullable=False),
            sa.Column("source", sa.String(60), nullable=False, server_default="seed"),
            sa.Column(
                "created_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.Column(
                "updated_at",
                sa.DateTime(timezone=True),
                server_default=sa.func.now(),
                nullable=False,
            ),
            sa.UniqueConstraint(
                "local_part", "category", name="uq_validation_local_rules_local_part_category"
            ),
        )

    if not _index_exists("ix_validation_local_rules_local_part"):
        op.create_index(
            "ix_validation_local_rules_local_part", "validation_local_rules", ["local_part"]
        )


def downgrade() -> None:
    op.drop_index("ix_validation_local_rules_local_part", table_name="validation_local_rules")
    op.drop_index(
        "ix_validation_local_rules_local_part_category", table_name="validation_local_rules"
    )
    op.drop_table("validation_local_rules")

    op.drop_index("ix_validation_domain_rules_domain", table_name="validation_domain_rules")
    op.drop_index(
        "ix_validation_domain_rules_domain_category", table_name="validation_domain_rules"
    )
    op.drop_table("validation_domain_rules")

    for name in (
        "ix_verification_jobs_tenant_created",
        "ix_verification_jobs_tenant_status",
    ):
        op.drop_index(name, table_name="verification_jobs")
    op.drop_table("verification_jobs")

    for name in (
        "ix_contacts_tenant_last_verified",
        "ix_contacts_tenant_risk_level",
        "ix_contacts_tenant_verification_score",
        "ix_contacts_tenant_verification_status",
    ):
        op.drop_index(name, table_name="contacts")
    for name, _column_type, _default in reversed(_CONTACT_VERDICT_COLUMNS):
        op.drop_column("contacts", name)
