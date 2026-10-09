from sqlalchemy import ForeignKey

from app.models import Base

EXPECTED_TABLES = {
    "tenants",
    "users",
    "roles",
    "permissions",
    "teams",
    "team_members",
    "role_permissions",
    "user_roles",
    "contacts",
    "contact_custom_fields",
    "contact_lists",
    "contact_list_members",
    "contact_tags",
    "contact_tag_members",
    "contact_sources",
    "import_jobs",
    "email_accounts",
    "provider_credentials",
    "provider_connections",
    "mailboxes",
    "sender_profiles",
    "domains",
    "domain_checks",
    "templates",
    "template_versions",
    "template_variables",
    "campaigns",
    "campaign_versions",
    "campaign_recipients",
    "scheduled_messages",
    "messages",
    "message_events",
    "threads",
    "replies",
    "suppressions",
    "unsubscribes",
    "bounces",
    "complaints",
    "ai_generations",
    "audit_logs",
    "webhooks",
    "usage_records",
    "usage_events",
    "tenant_subscriptions",
    "alert_records",
    "normalized_delivery_events",
    "ops_metric_samples",
    "refresh_tokens",
    "password_reset_tokens",
}


def test_expected_tables_are_registered() -> None:
    assert EXPECTED_TABLES <= set(Base.metadata.tables)


def test_tenant_owned_tables_have_tenant_id() -> None:
    # Platform-global tables hold no tenant data: system authN (roles), the
    # RBAC matrices, and the shared, versioned compliance policy documents
    # (tenant-level tracking lives in the tenant-scoped policy_acceptances).
    # The validation rulebook is global for the same reason: disposable /
    # free-provider / role-account conventions are public facts about DNS, not
    # tenant data. Tenant isolation is enforced on the *results* (contacts and
    # verification_jobs), not on this reference data.
    excluded = {
        "tenants",
        "permissions",
        "role_permissions",
        "compliance_policies",
        "validation_domain_rules",
        "validation_local_rules",
    }
    for table_name, table in Base.metadata.tables.items():
        if table_name not in excluded:
            assert "tenant_id" in table.c, table_name


def test_foreign_keys_are_declared() -> None:
    non_relational_ids = {
        "resource_id",
        "provider_message_id",
        "provider_thread_id",
        "provider_event_id",
        # RFC 5322 Message-ID header (not a relational FK).
        "message_id",
        "request_id",
        "external_event_id",
        # Provider-scoped external IDs (not relational DB FKs).
        "oauth_provider_account_id",
        "provider_account_id",
        "provider_mailbox_id",
        "external_thread_id",
        "external_message_id",
        "external_account_id",
        "external_sender_id",
        "external_identity_id",
        # Celery task identifier (not a relational FK).
        "celery_task_id",
        # App-level idempotency anchor for outbound_messages (a UUID minted by
        # the app, not a reference to another table).
        "correlation_id",
    }
    assert all(
        any(isinstance(constraint, ForeignKey) for constraint in column.foreign_keys)
        for table in Base.metadata.tables.values()
        for column in table.columns
        if column.name.endswith("_id")
        and column.name not in {"tenant_id", *non_relational_ids}
    )


def test_tenant_indexes_exist() -> None:
    for table in Base.metadata.tables.values():
        if "tenant_id" in table.c:
            assert (
                any("tenant_id" in {column.name for column in index.columns} for index in table.indexes)
            )
