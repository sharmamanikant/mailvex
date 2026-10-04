# Security Plan

## Threat Model

This application processes tenant-scoped customer data, sender credentials, provider OAuth flows, and outbound communications. The primary threats are:

- Cross-tenant data access through missing tenant scoping or IDOR.
- Credential leakage through logs, source control, browser storage, or insecure transport.
- OAuth and provider token abuse.
- Unsafe file imports or template rendering leading to data loss or code execution.
- Untrusted inbound email/reply content, contact metadata, and AI prompts causing prompt injection or unsafe rendering.
- CSRF or session abuse on browser-based cookie flows.
- Denial of service through authentication or upload abuse.

The mitigations below are enforced at the backend boundary and are the source of truth for production security.

## Security Objectives

Protect tenant data, sender authorization, provider credentials, recipient privacy, and platform availability while supporting legitimate business communication. Security controls belong in the backend and infrastructure; frontend controls are usability safeguards, not authorization boundaries.

## Identity and Access

- Hash passwords with a current adaptive password-hashing algorithm; never store plaintext passwords.
- Use short-lived JWT access sessions with secure refresh handling and revocation strategy.
- Enforce tenant membership and RBAC on every service and repository operation.
- Scope every tenant-owned query by authenticated tenant context before resource lookup.
- Provide roles and permissions for Super Admin, Admin, Team Manager, Campaign Manager, Sales/Recruiter, and Viewer.
- Add MFA support, recovery controls, login throttling, session invalidation, and audit events.
- Use secure, HTTP-only, same-site cookies when cookies are selected for browser sessions.

The implemented authentication boundary uses a short-lived HS256 JWT access token containing the user, tenant, role, expiry, and unique token ID claims. Refresh tokens are high-entropy opaque values stored only as SHA-256 hashes in `refresh_tokens`; rotation revokes the prior token and logout revokes the active token. Refresh tokens are delivered through an HTTP-only, same-site cookie and are never returned to frontend JavaScript.

Sender registration stores only an encrypted credential reference and encryption-key version. Provider adapters receive sender metadata and must resolve credentials through the server-side secrets boundary; no sender API or adapter accepts plaintext provider passwords or OAuth tokens.

Google credentials use an encrypted credential payload only after server-side OAuth exchange. The OAuth state is signed, tenant/user-bound, expires after ten minutes, and is single-use within the running service. Production deployments should back nonce consumption with shared Redis or another shared store before running multiple API instances.

Microsoft Entra credentials follow the same server-side boundary. Delegated Graph permissions are limited to `User.Read`, `Mail.Send`, and `offline_access`; access and refresh tokens are encrypted before persistence. Microsoft `429` and `503` responses are recorded as deferred-retry conditions and never trigger quota bypass or immediate retry loops.

SMTP passwords are encrypted through the credential store before persistence and are never logged, returned, or used by automated tests against external servers. SMTP connections validate TLS mode and use TLS, STARTTLS, or SSL transport explicitly; plaintext SMTP is not supported.

AI Message Studio uses an evidence-bounded provider interface and a development mock provider. Generation records store safe request summaries, provider/model metadata, token counts, estimated cost, and `DRAFT` workflow state. AI output cannot send automatically or bypass human review. Recipient and sender fields are treated as untrusted input, and frontend HTML previews must be sanitized.

ComplianceService is the mandatory send-time gate. It checks sender authorization/connection, recipient validation, suppression, unsubscribe state, campaign approval, sender/domain health, provider capacity, required unsubscribe mechanisms, and tenant policy. BLOCK results create an audit event and prevent provider invocation; only the compliance-gated SendingService can reach an adapter. A unique `(tenant_id, scheduled_message_id)` constraint prevents duplicate worker execution from creating duplicate outbound messages.

SuppressionService is the single tenant-wide suppression write boundary. Public unsubscribe tokens are stored hashed and immediately create `UNSUBSCRIBED` suppression. Hard bounces and complaints suppress at event-processing time; repeated temporary failures are quarantined as `POLICY_BLOCK`. All suppression lookups are tenant-scoped and are rechecked at send time.

Password authentication uses salted `scrypt` hashes with constant-time verification. Password reset tokens are stored hashed, expire after one hour, are single-use, and revoke existing refresh tokens after a successful reset. The reset request endpoint returns the same response for known and unknown accounts. MFA can be added as a separate server-side challenge/profile flow without changing the session contract.

FastAPI dependencies validate the JWT, load the active user using both user ID and tenant ID, and require permission checks through tenant-scoped role assignments. Missing credentials return `401`; insufficient permissions return `403`; resource ownership is checked in repositories/services to prevent IDOR and cross-tenant access.

## OAuth and Sender Credentials

- Use server-side OAuth authorization-code flows with state and PKCE where supported.
- Request only the least-privilege Gmail and Microsoft Graph scopes needed for approved features.
- Encrypt access/refresh tokens and SMTP credentials at rest using a key-management abstraction with key versioning and rotation.
- Keep provider secrets out of frontend JavaScript, URLs, logs, error responses, and analytics.
- Verify the connected provider profile matches the sender address before enabling sending.
- Support token refresh, reauthorization states, disconnect, credential deletion, and provider error handling.
- Never use account rotation or identity manipulation to evade provider restrictions.

## Sending and Compliance Controls

Every send operation must verify, at send time:

1. The sender is connected and authorized.
2. The campaign has passed human approval.
3. The recipient is valid and belongs to the tenant's authorized campaign scope.
4. The recipient is not suppressed, unsubscribed, hard-bounced, or complaint-blocked.
5. Sender/domain health and provider capacity permit sending.
6. Required unsubscribe and policy controls are present.

Suppression checks must be concurrency-safe and idempotent. Provider throttling causes delayed retry with bounded exponential backoff, never immediate retry loops. Adaptive scheduling may account for timezones, business hours, configured capacity, and queue health only.

## Data Protection

- Use TLS for all network traffic in deployed environments.
- Encrypt database backups, object storage, provider credentials, and other sensitive data at rest.
- Minimize retention of email bodies, imported personal data, provider payloads, and AI history.
- Separate credential records from sender metadata and restrict decryption capability.
- Do not expose internal IDs, raw provider payloads, stack traces, or sensitive fields unnecessarily.
- Define retention and deletion workflows for tenants, contacts, messages, events, files, and audit records.
- Treat imported contact fields and inbound email content as untrusted input.

## Input and File Security

- Validate request schemas, content types, lengths, pagination bounds, and upload sizes.
- Allow only approved CSV/XLSX formats and scan/parse them safely in workers.
- Guard against spreadsheet formula injection in exported error/report files.
- Escape rendered HTML and treat template variables as inert data substitution, never executable code.
- Sanitize filenames and store uploads through an object-storage abstraction outside the web root.
- Use parameterized SQL through SQLAlchemy and avoid dynamic query fragments from client input.
- Apply rate limits to authentication, imports, AI generation, public unsubscribe, and webhook endpoints.

## AI Security

- Send only the minimum necessary recipient and campaign context to an AI provider.
- Prevent models from inventing company facts, job openings, relationships, achievements, or technologies.
- Validate generated output and present it as a draft requiring human approval.
- Treat contact fields, campaign text, and inbound replies as possible prompt-injection content.
- Keep secrets, credentials, system prompts, and cross-tenant data out of model context.
- Record safe generation metadata for audit without retaining unnecessary personal data.

## Webhooks and External Providers

- Verify provider webhook signatures and timestamps where supported.
- Store provider event IDs and use idempotency keys to prevent duplicate processing.
- Preserve provider event semantics and isolate provider adapters from campaign business logic.
- Apply bounded retries and dead-letter handling for transient provider failures.
- Do not interpret provider acceptance as delivery or inbox placement.

## Application and Infrastructure Hardening

- Configure strict CORS, security headers, request size limits, and production-safe error responses.
- Terminate or forward HTTPS through Nginx with secure TLS configuration.
- Keep PostgreSQL and Redis on private networks; expose only required service ports.
- Run API, workers, scheduler, and supporting services with least-privilege accounts.
- Use health/readiness checks that reveal status but not secrets or internal diagnostics.
- Store configuration through environment variables or a secrets-management integration; commit only placeholders.
- Add structured security-relevant logging with request ID, tenant ID, user ID, operation, and outcome, excluding credentials and sensitive content.
- Prepare metrics and alerts for authentication failures, authorization denials, provider errors, queue failures, webhook failures, and unusual access patterns.

## Audit and Response

Audit immutable security-sensitive actions: login/logout, user and role changes, sender connect/disconnect, campaign approval/state changes, imports, AI generation, suppression/unsubscribe, settings changes, and credential lifecycle events. Provide restricted audit access and protect records from ordinary updates.

Document incident handling for credential compromise, tenant-isolation defects, provider abuse reports, data exposure, malicious uploads, and unauthorized sends. Revoke credentials, pause affected senders/campaigns, preserve relevant evidence, notify affected parties as required, and record remediation.

## Verification Plan

Security tests must cover tenant isolation, RBAC, authentication throttling, secure cookie/token behavior, suppression races, approval gating, unauthorized sender rejection, upload validation, template escaping, webhook verification, idempotency, error sanitization, and secret non-disclosure. Review controls at each implementation phase and perform dependency and configuration scans before production deployment.

## Audit Log

- **Phase 23 (2026-09-01) — Final Security Audit.** See `docs/security/security-audit-phase23.md` for the full report.
  - Fixed: AI reply drafts can no longer be sent unless `APPROVED` (inbox reply approval gate).
  - Fixed: `/ops/*` cross-tenant observability endpoints are now super-admin-only (`require_super_admin`); tenant admins receive `403`.
  - Fixed: Python `cryptography` vulns (pinned `>=50.0.0,<51`); `pip-audit` clean.
  - Fixed: Frontend `npm audit` to 0 vulnerabilities (`react-router-dom@7.18.3`, `vite@6.4.3`, `vitest@4.1.11`).
  - Fixed: removed committed dev private key `nginx/certs/tls.key` from git; private key / cert material is git-ignored. Regenerate dev certs with `infrastructure/nginx/gen_self_signed_certs.ps1`.
  - Verified safe: auth primitives, RBAC/tenant scoping, email suppression/compliance/webhook HMAC+replay+idempotency, AI evidence-bounded (never auto-send) behavior.
