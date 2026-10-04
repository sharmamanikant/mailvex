# Database Design

## Database Strategy

PostgreSQL is the durable system of record. SQLAlchemy 2.x models and Alembic migrations define schema changes. Public IDs use UUIDs. All tenant-owned records have `tenant_id`, and tenant indexes are included on high-volume access paths. Redis is not authoritative and is used for queues, delayed jobs, short-lived cache entries, locks, and idempotency windows.

Common columns on mutable entities: `id`, `tenant_id` where applicable, `created_at`, `updated_at`. Audit and event records are append-only. Store timestamps in UTC and retain recipient/sender timezone separately where scheduling needs it.

## Identity and Access

- `tenants`: organization identity, status, policy defaults.
- `users`: login identity, password hash, status, last-login metadata.
- `roles`: tenant or system role definitions.
- `permissions`: stable permission keys.
- `role_permissions`: role-to-permission mapping.
- `user_roles`: tenant-scoped user role assignments.
- `teams`: tenant teams.
- `team_members`: user/team membership.

Roles include Super Admin, Admin, Team Manager, Campaign Manager, Sales/Recruiter, and Viewer. Authorization is evaluated with tenant context in services/repositories.

## Contacts and Imports

- `contacts`: core recipient fields, validation status, suppression/unsubscribe status, source, and tenant ownership.
- `contact_custom_fields`: typed key/value fields per contact, unique by tenant/contact/key.
- `contact_lists`: named recipient lists.
- `contact_list_members`: list membership, unique by list/contact.
- `contact_tags`: tenant tags.
- `contact_tag_members`: contact/tag membership.
- `contact_sources`: normalized source definitions and references.
- `import_jobs`: upload metadata, mapping, status, counts, error-report location, and actor.

Recommended indexes: `(tenant_id, email)`, `(tenant_id, status)`, `(tenant_id, updated_at)`, and normalized email uniqueness per tenant. Do not assume one recipient email is globally unique across tenants.

Contact management currently supports normalized email uniqueness, status/source filtering, controlled sorting, custom fields from the approved recipient-context vocabulary, and tenant-scoped list/tag membership. Suppression and unsubscribe indicators are read-only contact state until the compliance phase owns their write paths.

## Senders, Providers, and Domains

- `email_accounts`: tenant sender connection, provider, address, display name, reply-to, status, and health score.
- `provider_credentials`: encrypted credential reference, key version, expiry metadata, and rotation timestamps; never store plaintext tokens.
- `sender_profiles`: sender name, company, designation, phone, signature, timezone, and policy configuration.
- `domains`: tenant domain and authorization metadata.
- `domain_checks`: check type, status, observed details, remediation, and checked timestamp.

Credentials are separated from sender metadata to restrict access and simplify key rotation.

## Templates and AI

- `templates`: logical template identity and tenant ownership.
- `template_versions`: immutable body/subject versions, status, author, and variable manifest.
- `template_variables`: declared variable names and source/type metadata.
- `ai_generations`: request inputs summary, model/provider metadata, generated output, validation result, actor, and campaign/template linkage. Avoid storing unnecessary personal data.

Variable rendering is data substitution only; template values are never evaluated as code.

## Campaign and Delivery

- `campaigns`: lifecycle state, objective, sender, current version, schedule policy, and ownership.
- `campaign_versions`: immutable campaign configuration snapshots and approval metadata.
- `campaign_recipients`: campaign/contact association, rendered-data reference, recipient state, and send eligibility.
- `scheduled_messages`: queue identity, due time, attempt count, priority, lock/idempotency data, and failure state.
- `messages`: provider-neutral message record, provider message/thread IDs, sender/recipient references, and current state.
- `message_events`: append-only normalized and provider-specific event types with event time and raw-reference metadata.

Use unique constraints for provider event IDs and message idempotency keys. Keep provider raw payloads minimized, encrypted or access-restricted where retention is required.

## Conversations

- `threads`: normalized provider thread, campaign/sender/recipient references, assignment and status.
- `replies`: inbound message content metadata, classification, approval state, and timestamps.

Attachment records should store metadata and object-storage references, not arbitrary blobs in PostgreSQL by default.

## Compliance and Reputation

- `suppressions`: tenant, recipient/email, reason, source, actor, and effective timestamp.
- `unsubscribes`: signed-token event, recipient, campaign/message context, and timestamp.
- `bounces`: message/provider event, classification, diagnostic metadata, and timestamp.
- `complaints`: provider complaint event and classification metadata.
- `audit_logs`: immutable tenant-scoped actor/action/resource records with request ID and safe metadata.
- `webhooks`: provider, external event ID, signature status, processing status, and retry metadata.
- `usage_records`: tenant/user metric, period, quantity, and source.

Suppression lookup must support normalized email and contact ID, and must be transactionally safe against concurrent send attempts.

## Constraints and Indexing

- Foreign keys use restrictive or explicit cascade behavior; deleting a tenant is an administrative workflow, not an accidental cascade.
- Every tenant query filters by `tenant_id` before resource ID lookup.
- Add composite indexes for common tenant filters and queue due-time/state scans.
- Use check constraints or typed enums for lifecycle/status fields, while preserving provider-specific event values separately.
- Protect immutable versions, events, audit logs, and approved snapshots from ordinary update paths.
- Use optimistic locking or state-transition guards for campaign pause/resume/approval and sender status changes.

## Migration Order

1. Tenants, users, roles, permissions, teams.
2. Contacts, custom fields, lists, tags, sources, imports.
3. Email accounts, credentials, sender profiles, domains, checks.
4. Templates, versions, variables, AI generations.
5. Campaigns, versions, recipients, scheduled messages, messages, events.
6. Threads, replies.
7. Suppression, unsubscribe, bounce, complaint, webhook, audit, usage tables.
8. Additional indexes, constraints, and performance migrations after measured access patterns.

This is a logical design baseline. Exact SQLAlchemy types, enum strategy, retention periods, and partitioning should be finalized during Phase 2 after the first API access patterns are known.

## Scheduler Implementation

`scheduled_messages` stores due time, priority, attempt count, maximum attempts, deferred-until timestamp, failure reason, processed timestamp, and an idempotency key. SchedulerService calculates recipient-local business-hour delivery times, checks campaign approval and sender health, and uses tenant-scoped queries. Provider throttling is represented as deferred work; after bounded exponential backoff or a provider-supplied retry delay, exhausted work becomes failed/dead-letter state. No sender rotation or anti-detection timing is implemented.

## Campaign Manager Implementation

`campaigns` stores the tenant-owned sender, template version, lifecycle status, schedule policy, follow-up policy, creator, and approval metadata. `campaign_recipients` stores tenant-scoped recipient membership. `campaign_versions` stores immutable approval snapshots containing campaign configuration and recipient IDs. The campaign service validates sender, template, list, and contact ownership before persistence and never delegates sending directly.

## Template Engine Implementation

Templates are tenant-scoped and use immutable version rows. A template stores lifecycle status (`DRAFT`, `ACTIVE`, or `ARCHIVED`) and a pointer to its current version. Each version stores subject, HTML body, optional plain-text body, detected variable manifest, and declared tenant-defined variables. Rendering performs inert placeholder substitution with HTML escaping; it never evaluates template text as code. Missing values produce warnings and empty substitutions in previews.

## Implemented Phase 2 Baseline

The initial SQLAlchemy registry is implemented in `backend/app/models/`. It includes the requested tables plus `role_permissions`, `user_roles`, and `contact_tag_members` association tables required by the relationships. Shared UUID primary keys, UTC-aware timestamp columns, tenant ownership, PostgreSQL JSONB variants, foreign keys, indexes, and uniqueness constraints are defined centrally.

`backend/app/alembic/versions/20260823_01_initial_schema.py` is the initial migration. `20260823_02_authentication.py` explicitly backfills refresh and password-reset token tables for databases that already applied the initial revision. Both revisions can be upgraded or downgraded through Alembic. A later migration should replace metadata-driven creation in the initial baseline with frozen operation directives before production schema evolution begins, so historical migrations remain independent of future model changes.

Tenant isolation is enforced in `TenantScopedRepository` and `TenantAccessService` through mandatory tenant context and ownership checks. Cross-table tenant consistency should be validated by application services until composite tenant foreign keys are introduced for relationships that require database-level enforcement.

Provider credentials contain only an encrypted credential reference and encryption-key version. Passwords are represented as `password_hash` on users; no plaintext password, OAuth token, or SMTP secret column exists.
