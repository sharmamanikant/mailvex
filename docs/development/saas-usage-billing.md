# SaaS Usage & Billing (Phase 20)

A tenant-scoped **usage metering plus plan-limits foundation**: a data-driven plan catalog, an immutable per-tenant usage-event log, and limit enforcement that raises clear, non-silent errors. It sets the guardrails so nothing polling bypasses suppression, compliance, provider-throttling, or security gates.

## Design principles

- **Meter, don't gate everything.** Only capacity/plan limits are enforced at request time (contacts, AI generations, campaigns, connected senders, storage, team seats). Delivery volume (`messages`) is *recorded, never hard-gated* — compliance and provider throttling stay the priority send-time gates. `UsageService.enforce` is always consulted; `record_event` never throws a limit error.
- **Immutable events + reservation ledger.** Every metering action writes a `UsageEvent` (append-only: tenant, actor, type, period, quantity, resource). Capacity claims for period metrics are reserved in a `UsageRecord` via a **single atomic, guarded upsert** — `INSERT … ON CONFLICT DO UPDATE … WHERE total <= limit` — so concurrent reservations can never silently overshoot on SQLite or PostgreSQL. Absolute metrics (contacts, senders, storage, seats) are checked against **live row counts** under a tenant-row lock (`SELECT … FOR UPDATE`), so "usage" always reflects actual workspace standing, not just events.
- **No hard-coded limits.** Pricing/capacity live only in `plans.py`; code references plan *codes* and *metric keys* by constant. `PlanLimit.scope` is derived (`absolute` = live standing, `period` = resets each billing period, default monthly).
- **Default-free.** A tenant with no subscription row consistently reads the Free plan; plan data falls back gracefully.
- **Clear errors, client-visible.** Over-limit requests raise `UsageLimitError(metric, used, limit, plan)` → HTTP 409 `{"detail", "code": "usage_limit", "metric", "used", "limit", "plan"}`. Importers pre-check headroom *per chunk* and surface the quota in the same shape.

## Catalog (`backend/app/billing/plans.py`)

Metrics: `contacts`, `ai_generations`, `campaigns`, `messages`, `connected_senders`, `storage_mb`, `team_members`. Absolute: `contacts`, `connected_senders`, `storage_mb`, `team_members`; the rest are period.

- Free: 1,000 contacts · 200 AI generations /mo · 20 campaigns /mo · 5,000 messages /mo · 2 senders · 5 GB storage · 2 seats.
- Enterprise: `None` (unlimited) on every metric.

Event types (`EVENT_*` constants): `CONTACT_CREATED`, `AI_GENERATION`, `CAMPAIGN_CREATED`, `MESSAGE_SENT`, `SENDER_CONNECTED`, `STORAGE_USED`, `TEAM_MEMBER_ADDED`; `EVENT_TO_METRIC` maps types to metrics; `METRIC_LABELS` drives displays.

## Service (`backend/app/billing/usage.py`)

`UsageService(session, tenant_id, actor_id=None)`:

- `enforce(metric, quantity=1)` — raise `UsageLimitError` if no headroom (absolute: lock + live count; period: guarded atomic `_reserve`). No-op when the limit is unlimited.
- `meter(event_type, quantity, resource_type, resource_id, metadata)` — enforce then record (caller's transaction; a raised error persists nothing). Returns the `UsageEvent`.
- `record_event(...)` — record **without** enforcing (used by the delivery-send paths and storage uploads).
- `check(metric)` — read-only `{used, limit, remaining, scope}` headroom.
- `envelope()` — serializable `{tenant_id, plan_code, plan_name, price_usd_mo, features, status, period_start, period_end, metrics[]}` where each metric is `{metric, label, used, limit, remaining, scope}`.
- `effective_limits()` — plan limits overridden by subscription `custom_limits` (and seat-limited `team_members` on paid plans).
- `change_plan(plan_code, reset_period, status)` (validated) → new subscription, refreshed `period_start` on reset, `PLAN_CHANGED` audit row.
- `recent_events(event_type=None, limit=1..500)` — newest-first, tenant-scoped.
- `month_start()` / `month_end()` helpers for period boundaries.

## Models (`backend/app/models/entities.py`)

- `TenantSubscription` → `tenant_subscriptions`: `plan_code`, `status`, `seats`, `custom_limits` (JSON), `period_start`, `period_end`; unique on `tenant_id`; missing row ⇒ Free/ACTIVE/defaults.
- `UsageEvent` → `usage_events`: immutable; `event_metadata` Python attribute mapped to the `metadata` column (`metadata` is reserved in the Declarative API — same pattern as `AuditLog.audit_metadata`). Columns: id, tenant_id, actor_id, event_type, period_start, quantity, resource_type, resource_id, metadata, created_at. Indexed on `(tenant_id, period_start, event_type)`.
- `UsageRecord` → `usage_records`: reservation ledger keyed `(tenant_id, metric, period_start)`.

Migration: `alembic/versions/20260901_26_usage_billing.py` (revision `20260901_26`, down `20260901_25`), guarded by `_table_exists()`, JSONB on PostgreSQL / JSON otherwise.

## Wiring

- `services/auth.py` signup creates the default Free `TenantSubscription`, seeds `billing.read`/`billing.manage` permissions, meters `TEAM_MEMBER_ADDED`; `core/database.py` dev seed adds the subscription + permission keys.
- Meters: `contacts.py` (contact created), `campaigns.py`, `ai.py` `generate_email`, `ai_assistant.py` (draft when error is None, summarize/next_action/transform), `inbox_reply.py` draft, `ai_drafts.py` (`generate` meters *before* the provider call, so the plan gate precedes provider spend), `google_oauth.py` / `microsoft_oauth.py` / `senders.py` (`SENDER_CONNECTED`), `imports.py` (`enforce_contact_headroom(len(chunk))` before each chunk + per-row `CONTACT_CREATED`), `api/imports.py` upload (`STORAGE_USED`, bytes→MB), `api/admin.py` user creation (seat limit on `EVENT_TEAM_MEMBER_ADDED`).
- Delivery: `sending.py` records `MESSAGE_SENT` after provider success in both `send_scheduled` and `send_delivery_job` (record only).
- `main.py`: `UsageLimitError` → 409 handler; `usage_router` mounted at `/api/v1`.

## API (`backend/app/api/usage.py`)

Gated by `billing.read` (PATCH plan by `billing.manage`) under `/api/v1/usage`:

- `GET /overview` — envelope (current usage vs limits + plan info).
- `GET /plans` — full catalog (`code`, `name`, `price_usd_mo`, `features`, `limits`).
- `GET /limits` — effective limits with scope.
- `GET /events?event_type&limit=` — recent immutable events (unknown type → 400, `limit` validated 1..500).
- `PATCH /plan` `{plan_code, reset_period, status}` — change plan (unknown code → 400).
- `POST /events` — manual `billing.manage` metering → 201 (+ audit `USAGE_EVENT_RECORDED`); unknown event type → 400.

## Frontend

- `types/billing.ts` — `UsageOverview`, `UsageMetric`, `BillingPlan`, `PlanLimitRow`, `UsageEventRow`.
- `api/billing.ts` — `usageApi.overview/plans/events` (bearer-token fetch, mirrors `reporting.ts`).
- `pages/Usage.tsx` — **Usage & plan** page at `/usage` (nav "Usage", `billing.read` permission): current-plan card (name, price, billing period, features), per-metric meters with used/limit, remaining, and a progress bar (turns amber at ≥90%), an available-plans panel, and a recent-activity event table. Storage displayed as GB; `unlimited` for `None` limits. Styles in `styles.css` (`.usage-plan`, `.usage-meter`, `.usage-bar`, `.plan-list`, …).

## Tests

- `backend/tests/test_usage_billing.py` — periodic limit enforced (exact raise at the boundary), absolute limit enforced against live rows, zero-limit plans, concurrency (threaded reservations never overshoot), tenant isolation, plan change + `PLAN_CHANGED` audit + fresh period, envelope `used/limit/remaining` math, messages tracked-but-never-gated, import headroom pre-check, and the `ContactService` integration path.
- `backend/tests/test_usage_api.py` — authed `TestClient` with `billing.read`/`billing.manage` roles: overview envelope, plans, limits, events list, manual event 201, unknown event type 400, plan PATCH 200/400, and permission rejection (viewer → 403).
- `frontend/src/pages/Usage.test.tsx` — heading + current plan, per-metric meters, available-plans pricing, recent-activity table.

Backend suite fully green (418 passed); ruff and mypy clean on the new/edited files (only pre-existing `permissions.py:58` no-untyped-def remains); frontend `npm run build`, `npm run lint`, and `npm run test` (15 tests) all green.