# API Design

## Conventions

Base path: `/api/v1`.

- JSON request and response bodies use typed Pydantic schemas.
- Authentication uses secure HTTP-only cookies or bearer tokens according to the final frontend session design; provider tokens are never returned.
- Resource IDs are UUIDs.
- List endpoints use `page`, `page_size`, stable sorting, and maximum page sizes.
- Mutations return the resource or an operation/status object appropriate to asynchronous work.
- Errors use a consistent envelope with `code`, `message`, `request_id`, and safe field details.
- Every request carries or receives a request ID; logs include tenant, user, service, operation, status, and duration.
- Tenant context comes from authenticated membership and is never accepted as an arbitrary client-controlled override.

## Authentication and Administration

```text
POST /auth/login
POST /auth/logout
POST /auth/refresh
GET  /auth/me
POST /auth/register
POST /auth/forgot-password
POST /auth/reset-password
POST /auth/password-reset/request
POST /auth/password-reset/confirm
POST /auth/mfa/challenge
POST /auth/mfa/verify
GET  /users
POST /users
PATCH /users/{id}
GET  /roles
GET  /teams
POST /teams
GET  /audit-logs
```

All administrative routes require permissions such as `settings.manage`, `audit.read`, or role-specific user-management permissions.

`/auth/login` returns a short-lived access token and sets an HTTP-only refresh cookie. `/auth/refresh` rotates that cookie and returns a new access token. `/auth/logout` revokes the stored refresh-token hash. `/auth/me` requires a valid bearer token. Password-reset request responses are account-enumeration safe; reset tokens are single-use and hashed at rest.

## Contacts and Imports

```text
GET    /contacts
POST   /contacts
GET    /contacts/{id}
PATCH  /contacts/{id}
DELETE /contacts/{id}
POST   /contacts/bulk
POST   /contacts/import
GET    /imports/{id}
GET    /imports/{id}/errors
GET    /lists
POST   /lists
GET    /lists/{id}
POST   /lists/{id}/members
GET    /segments
GET    /suppressions
POST   /suppressions
```

Import creation validates file type and size, then returns an async job. Mapping, preview, row validation, deduplication, and final import are separate state transitions.

`GET /contacts` supports bounded pagination, controlled sorting, search across name/email/company, and status/source filters. Contact mutations are tenant-scoped and require the corresponding `contacts.*` permission. Bulk actions are limited to tenant-owned contact IDs and tenant-owned list/tag targets.

## Senders and Domains

```text
GET    /senders
GET    /senders/{id}
POST   /senders
POST   /senders/{id}/disconnect
POST   /senders/{id}/health-check
POST   /senders/{id}/test-connection
POST   /senders/{id}/test-email
POST   /senders/{id}/enable
POST   /senders/google/connect
GET    /senders/google/callback
POST   /senders/microsoft/connect
GET    /senders/microsoft/callback
POST   /senders/smtp
POST   /senders/{id}/test
DELETE /senders/{id}
GET    /domains
POST   /domains
GET    /domains/{id}/health
POST   /domains/{id}/checks
```

OAuth callbacks validate state/PKCE as applicable. Credentials are stored server-side and all provider operations occur through adapters.

The current Sender Center supports tenant-scoped sender registration, listing, detail, disconnect, and health-check operations. Sender creation accepts only an encrypted credential reference and key version; raw OAuth tokens and provider passwords are never accepted by the API. Google and Microsoft OAuth flows are exposed through server-side callbacks, while mock adapters remain available for tests and local development.

Google OAuth endpoints are `GET /senders/google/connect`, which returns an authorization URL, and `GET /senders/google/callback`, which exchanges the code server-side, retrieves the Gmail profile, and creates the sender. The frontend never receives OAuth client secrets or provider tokens.

Microsoft Entra OAuth endpoints are `GET /senders/microsoft/connect` and `GET /senders/microsoft/callback`. The callback exchanges the authorization code server-side, validates the mailbox through Microsoft Graph `/me`, and stores the token response encrypted. Graph throttling responses are surfaced with retry metadata for deferred scheduler handling.

SMTP sender configuration uses `smtp_host`, `smtp_port`, `smtp_tls_mode` (`TLS`, `STARTTLS`, or `SSL`), and `smtp_username`. The SMTP password is accepted only during creation, encrypted immediately, and never returned. Test connection, test email, enable, disable, and delete operations are tenant- and permission-scoped.

## Templates and AI

```text
GET  /templates
POST /templates
GET  /templates/{id}
POST /templates/{id}/versions
POST /templates/{id}/preview
POST /templates/{id}/duplicate
POST /templates/{id}/status
POST /ai/generate-email
POST /ai/generations/{id}/transform
GET  /ai/generations/{id}
```

AI Message Studio requests include objective, audience, supplied context, service/product, tone, language, CTA, sender information, recipient fields, and optional custom values. Generation and transformation responses are always `DRAFT`; they contain subject, body, CTA, personalization suggestions, optional follow-up, warnings/metadata, and token/cost measurements. No AI endpoint sends or approves a message.

Template endpoints support tenant-scoped creation, editing as a new version, duplication, lifecycle status changes, and per-recipient preview. Preview returns rendered subject, HTML, plain text, used variables, missing variables, and warnings. Built-in variables are fixed by the template service; tenant-defined variables must be declared before use.

Generation requests include objective, audience, supplied context, tone, language, CTA, and sender profile. The API returns draft output plus warnings and validation metadata. It does not approve or send a campaign.

## Campaigns

```text
GET  /campaigns
POST /campaigns
GET  /campaigns/{id}
PATCH /campaigns/{id}
POST /campaigns/{id}/preview
POST /campaigns/{id}/approve
POST /campaigns/{id}/schedule
POST /campaigns/{id}/pause
POST /campaigns/{id}/resume
POST /campaigns/{id}/cancel
POST /campaigns/{id}/duplicate
GET  /campaigns/{id}/recipients
GET  /campaigns/{id}/analytics
```

Campaign creation requires a tenant-owned sender and template plus tenant-owned recipient IDs or a non-empty contact list. Campaigns begin in `DRAFT`; status transitions are guarded by the campaign service. Approval is only possible from `REVIEW`, requires recipients and a template, records the approver, and creates an immutable campaign snapshot. No campaign operation sends email.

Scheduler operations are tenant-scoped and approval-gated: `POST /campaigns/{id}/schedule` creates delayed `scheduled_messages` only when the campaign is `APPROVED`; pause, resume, and cancel update future queue work without retracting provider-accepted messages. Queue state is `QUEUED`, `PROCESSING`, `SENT`, `DEFERRED`, `FAILED`, or `CANCELLED`.

Approval is a guarded transition that records approver/time and creates an immutable campaign snapshot. Schedule requires approved state. Pause blocks future queue work; it does not retract provider-accepted messages.

## Conversations and Analytics

```text
GET  /conversations
GET  /conversations/{id}
POST /conversations/{id}/suggest-reply
POST /conversations/{id}/reply
POST /conversations/{id}/assign
GET  /analytics/dashboard
GET  /analytics/campaigns/{id}
GET  /analytics/senders/{id}
```

The send worker invokes ComplianceService before template rendering or provider access. Compliance can be inspected through `GET /compliance/campaigns/{campaign_id}/recipients/{recipient_id}`; the response includes every check, its `PASS`, `WARN`, or `BLOCK` outcome, and remediation guidance.

Suggested replies remain drafts until human approval. Analytics distinguish queued, sent, provider-accepted, delivered, bounced, failed, replied, unsubscribed, and complaint events.

## Public Compliance Endpoints

```text
GET  /unsubscribe/{token}
POST /unsubscribe/{token}
GET  /preferences/{token}
```

`GET /unsubscribe/{token}` is public and idempotently writes a tenant-wide `UNSUBSCRIBED` suppression. Bounce and complaint processors use the same suppression service: `HARD_BOUNCE` and complaints suppress immediately; `TEMPORARY_FAILURE` returns retry until the configured threshold, then creates a `POLICY_BLOCK` quarantine suppression.

Tokens are signed, scoped, expiring where policy permits, and do not expose internal IDs unnecessarily. Unsubscribe processing writes suppression immediately and is idempotent.

## Internal and Webhook Boundaries

Provider webhooks use dedicated endpoints outside ordinary user resource routes, verify signatures, store an idempotency key, and enqueue processing:

```text
POST /webhooks/{provider}
```

Worker tasks are not directly exposed as public HTTP endpoints. Health and readiness endpoints are operational-only and must not reveal secrets or stack traces.

## Authorization Matrix

- Contacts: `contacts.read/create/update/delete`.
- Campaigns: `campaigns.read/create/update/approve/pause`.
- Senders: `senders.read/connect/disconnect`.
- Templates: `templates.read/create/update`.
- Analytics: `analytics.read`.
- Administration: `settings.manage`, `audit.read`, and explicit user/team permissions.

Every service method receives authenticated tenant and actor context. Object-level authorization is checked after tenant scoping and before mutation.

## Async Operation Contract

Long-running imports, validation, AI generation where needed, campaign preparation, scheduling, sending, event processing, and analytics aggregation return an operation/job ID with status fields such as `QUEUED`, `RUNNING`, `SUCCEEDED`, `FAILED`, and `CANCELLED`. Retryable failures are represented without exposing provider credentials or internal traces.

## Documentation and Compatibility

FastAPI-generated OpenAPI is the API reference. Each endpoint should document permissions, request/response schemas, pagination, status codes, idempotency behavior, and provider-specific limitations. Contract tests should protect the frontend-facing schema as features are added.
