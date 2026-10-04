# Suppression and Delivery Events (Phase 14)

Phase 14 introduced the authoritative per-tenant `suppression_entries` safety
control plus a normalized, idempotent delivery-event pipeline.

## Suppression engine (`SuppressionEntry`)

`SuppressionEngine` (`app/services/suppression_engine.py`) is the single global
gate that every send path consults immediately before delivery.

- Table: `suppression_entries`, unique per `(tenant_id, email_normalized)`.
- Emails are always normalized (strip + lowercase) via
  `app.utils.emails.normalize_email`.
- Types: `UNSUBSCRIBED`, `HARD_BOUNCE`, `COMPLAINT`, `MANUAL`, `ADMIN_BLOCKED`.
- Provider-derived types (`COMPLAINT`, `HARD_BOUNCE`, `UNSUBSCRIBED`) are
  **protected** and cannot be removed through the operator API (HTTP 403) unless
  `force=True`.
- `ComplianceService.check_recipient(...)` is the final pre-delivery recheck. On
  a `BLOCK` it marks `CampaignRecipient.eligibility_status = "SUPPRESSED"` and
  the message is not delivered. `SendingService.send_scheduled` calls this gate.
- Compliance evaluates suppression across **both** the modern `suppression_entries`
  table and the legacy `suppressions` table. The legacy `SuppressionService` is
  preserved for backward compatibility; `suppression_entries` is authoritative.

## Delivery events (`NormalizedDeliveryEvent`)

`DeliveryEventService` (`app/services/delivery_events.py`) normalizes raw provider
events into canonical types and applies safety side effects exactly once.

- Table: `normalized_delivery_events`, unique per `(tenant_id, provider,
  provider_event_id)` → idempotent even if a provider redelivers the same event.
- Canonical types: `DELIVERED`, `TEMPORARY_FAILURE`, `HARD_BOUNCE`, `COMPLAINT`,
  `UNSUBSCRIBED`, `UNKNOWN`. Provider labels are mapped via `_PROVIDER_CANONICAL`
  for `GOOGLE`, `MICROSOFT`, and `SMTP`.
- Side effects:
  - `DELIVERED` → message marked `DELIVERED` + campaign statistic.
  - `HARD_BOUNCE` → `SuppressionEntry(HARD_BOUNCE)` + message `BOUNCED`.
  - `COMPLAINT` → `SuppressionEntry(COMPLAINT)` + message `COMPLAINED`.
  - `UNSUBSCRIBED` → `SuppressionEntry(UNSUBSCRIBED)`.
  - `TEMPORARY_FAILURE` → message `DEFERRED`; only suppresses (`MANUAL`) after a
    configurable repeated-failure threshold (default 3).
  - `UNKNOWN` → message event only, never suppresses.
- A duplicate delivery event id never re-applies suppression, statistics,
  analytics, or audit.

## Webhook endpoint

`POST /api/v1/delivery-events/{provider}/{tenant_id}`:

1. Signature verification (`X-Event-Id`, `X-Timestamp`, `X-Signature`) via
   `WebhookService` — HMAC-SHA256 over `"{timestamp}.{event_id}"` with the
   provider secret. Replay window is 300s. Unverified webhooks are rejected and
   never modify state.
2. Payload normalization and idempotent processing.
3. `GET /api/v1/delivery-events/types` exposes canonical types + provider labels.

## Public unsubscribe

The public (session-less, backend-rendered) pages serve outside `/api/v1`:

- `GET /unsubscribe/{token}` → confirmation page.
- `POST /unsubscribe/{token}/confirm` → writes `SuppressionEntry(UNSUBSCRIBED)`
  and the legacy unsubscribe record.
- `SuppressionService.build_unsubscribe_url(...)` builds links using
  `PUBLIC_BASE_URL`.

## Configuration (required at deploy time)

Two settings are read from environment and NOT baked in:

- `WEBHOOK_SECRETS` — JSON-ish `provider=secret` pairs parsed by
  `app.core.config._webhook_secrets` into `settings.webhook_secrets`. Providers
  without a configured secret are rejected as "not configured". Set one secret
  per provider you receive events for (keys are uppercased, e.g. `GOOGLE`,
  `MICROSOFT`, `SMTP`).
- `PUBLIC_BASE_URL` — public origin used to build unsubscribe links (default
  `http://localhost:8000`). Point it at the externally reachable host.

## Validation

Covered by `tests/test_suppression_engine.py`,
`tests/test_delivery_events.py`, and `tests/test_webhook_endpoint_flow.py`
(plus the pre-existing suppression/compliance/sending/webhook suites). Full
backend suite: 304 passing. Ruff clean; mypy clean for Phase 14 files (the only
remaining strict-mode error is the pre-existing `app/security/permissions.py`
missing return annotation, unrelated to this phase).
