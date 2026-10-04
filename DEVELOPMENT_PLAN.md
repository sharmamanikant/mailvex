# Development Plan

## Principles

- Implement one vertical phase at a time.
- Inspect, implement, test, lint/type-check, document, and report each phase.
- Keep provider-specific logic behind adapters.
- Never send a newly generated campaign without explicit human approval.
- Keep tenant authorization in backend services and repositories, not only in the UI.
- Use mock providers for local development and tests; never add real credentials.

## Exact Implementation Sequence

### Phase 1: Project Scaffolding

Create the monorepo layout, frontend and backend bootstrap, shared environment template, Docker Compose services, health endpoints, lint/type-check/test commands, and foundational documentation. Establish configuration loading, structured logging, request IDs, and error handling.

Exit criteria: frontend and API boot locally; PostgreSQL and Redis health checks pass; no secrets are committed.

### Phase 2: Database and Migrations

Add SQLAlchemy models, Alembic baseline, UUID identifiers, timestamps, tenant indexes, foreign keys, uniqueness constraints, and the minimum tables in `DATABASE_DESIGN.md`.

Exit criteria: clean migration up/down cycle and model tests.

### Phase 3: Authentication and RBAC

Implement password hashing, JWT authentication, tenant membership, roles, permissions, authorization dependencies, audit events, rate limiting hooks, and MFA extension points.

Exit criteria: authentication and RBAC tests, including cross-tenant denial.

### Phase 4: Contact Management

Implement contacts, custom fields, tags, lists, segments, source tracking, search, filters, sorting, pagination, suppression/unsubscribe indicators, and CRUD APIs/UI.

### Phase 5: Import and Validation

Implement CSV/XLSX upload validation, async import jobs, mapping/preview/report flow, deduplication, syntax/domain/MX/disposable/role/suppression checks, and downloadable error reporting.

### Phase 6: Sender Architecture

Implement sender profiles, encrypted credential storage abstraction, provider interface, connection state, health state, and mock provider.

### Phase 7: Google OAuth and Gmail

Implement authorization, callback, token refresh, profile retrieval, connection validation, disconnect, send, message/thread retrieval, throttling, and least-privilege scopes.

### Phase 8: Microsoft Graph

Implement Entra OAuth, token lifecycle, profile and sender verification, send mail, thread/message retrieval where supported, disconnect, and throttling handling.

### Phase 9: SMTP

Implement TLS/STARTTLS/SSL configuration, connection test, encrypted credentials, send, reply-to, and safe error handling without password logging.

### Phase 10: Templates and Variables

Implement versioned templates, variable detection/validation, custom fields, missing-variable warnings, HTML plus plain-text rendering, preview, and safe non-executable substitution.

### Phase 11: AI Message Studio

Implement provider abstraction, evidence-bounded prompts, generation history, regenerate/edit actions, tone/language transforms, preview, and output validation. AI creates drafts only.

### Phase 12: Campaign Engine

Implement campaign versions, recipient selection, mapping, lifecycle states, preview, duplicate, follow-up configuration, approval, immutable approved snapshots, and UI workflow.

Implemented baseline: tenant-scoped campaign CRUD, recipient list/ID validation, guarded lifecycle transitions, immutable approval snapshots, duplication, campaign detail UI, and no-send scheduler handoff.

### Phase 13: Scheduler and Queue

Implement scheduled messages, business hours, recipient timezones, policy/capacity limits, priority, delayed jobs, retry backoff, dead-letter handling, pause/resume, and idempotency.

### Phase 14: Compliance Engine

Implement PASS/WARN/BLOCK checks for authorization, recipient validity, suppression, approval, health, capacity, unsubscribe/policy requirements, and a pre-send decision record.

### Phase 15: Bounce, Unsubscribe, and Suppression

Implement signed unsubscribe tokens/endpoints, suppression reasons, hard/soft bounce classification, complaint handling, quarantine policy, and send-time enforcement.

### Phase 16: Replies and Conversations

Implement provider ingestion, normalized threads/replies, assignment, tags, notes, attachments metadata, webhook idempotency, and AI classification with human-approved suggested replies.

### Phase 17: Analytics

Implement event aggregation, campaign/sender/team/time dimensions, delivery/bounce/reply/positive-reply/unsubscribe/complaint metrics, and dashboard APIs/UI. Do not use open rate as the primary KPI.

### Phase 18: Domain and Sender Health

Implement SPF, DKIM, DMARC, MX, TLS and observable authorization checks, health scoring, remediation guidance, sender warning controls, and critical sender campaign pause.

### Phase 19: Security Hardening

Add security headers, strict CORS, CSRF protection where applicable, upload limits and validation, payload limits, error sanitization, secret-management integration, webhook verification, and security review.

### Phase 20: Testing

Complete unit, API, integration, provider-mock, scheduler, compliance, tenant-isolation, RBAC, security, frontend, and E2E coverage. Include every test listed in the product specification.

### Phase 21: Docker Deployment

Finalize Compose health checks, dependency ordering, persistent volumes, worker/scheduler commands, Nginx configuration, production environment guidance, backups, and deployment scripts.

### Phase 22: Production Documentation

Complete README, architecture, database, API, development, deployment, security, compliance, OAuth, provider, operations, and incident guidance.

## Required Validation Per Phase

1. Inspect the affected code and neighboring tests.
2. Implement the smallest coherent slice.
3. Run focused tests.
4. Fix local failures.
5. Run lint and type checks.
6. Update relevant documentation.
7. Report changed files, verified behavior, and remaining work.

## First Implementation Slice

Start with Phase 1 only: scaffold the monorepo, establish configuration and health checks, add Docker Compose development services, and create a minimal smoke-test path. Do not add real integrations or sending behavior in the scaffold phase.
