# Business Reporting & Dashboard (Phase 19)

A tenant-scoped **business reporting** center that answers the classic deliverability questions: how many contacts do we have, which campaigns are active/scheduled/completed, what did we actually send, how many messages reached the inbox (delivered), bounced, were blocked, drew complaints, or triggered unsubscribes — and which senders are healthy versus need attention. All reports honor an optional date window and export to CSV.

## Design principles

- **Aggregated, not row-scanned.** Every count is produced with `GROUP BY` SQL (a handful of queries per report) rather than per-message / per-campaign counting in N+1 loops. The dashboard never iterates individual rows.
- **Outcomes come from the de-duplicated `NormalizedDeliveryEvent` table.** Delivered / hard bounce / complaint / unsubscribe / temporary failure are derived from provider-normalized events (idempotent on `(tenant_id, provider, provider_event_id)`).
- **The send-path funnel comes from the idempotent `DeliveryJob` queue** (one row per recipient per campaign). For tenants that never used the delivery queue, counts fall back to the legacy `Message.status` table.
- **Strict tenant isolation.** Every query filters on `tenant_id` captured from the authenticated principal; cross-tenant leakage is covered by tests.
- **No premature complexity.** Reports use a handful of tuned queries per request; no background roll-up tables, no per-partition caching. If a dataset grows to millions of rows, the same query shapes already group server-side and are indexed on `(tenant_id, campaign_id, status)` / `(tenant_id, message_id)`.

## Metric semantics (status values)

| Bucket | Source | Statuses counted |
|---|---|---|
| Campaign: active | `Campaign.status` | `RUNNING, PAUSED` |
| Campaign: scheduled | `Campaign.status` | `SCHEDULED, APPROVED` |
| Campaign: completed | `Campaign.status` | `COMPLETED` |
| Delivery: sent | `DeliveryJob.status` | `SENT, DELIVERED` (fallback: `Message.status` `SENT, ACCEPTED, DELIVERED`) |
| Delivery: blocked / failed | `DeliveryJob.status` | `BLOCKED` / `FAILED` |
| Delivery: throttled | `DeliveryJob.failure_code` | `PROVIDER_THROTTLED` |
| Delivery: delivered / bounced / complaints / unsubscribed / temp. failure | `NormalizedDeliveryEvent.event_type` | `DELIVERED` / `HARD_BOUNCE` / `COMPLAINT` / `UNSUBSCRIBED` / `TEMPORARY_FAILURE` |
| Sender: connected | `EmailAccount.status` | all except `DISABLED, DISCONNECTED, SUSPENDED, REAUTH_REQUIRED, HEALTH_CRITICAL` |
| Sender: healthy | `EmailAccount` | `health_score >= 80` (non-unavailable) or status `HEALTHY` |
| Sender: needs attention | `EmailAccount` | unavailable statuses |
| Contact | `Contact.status` / `validation_status` / `suppression_status` | `ACTIVE`, `INACTIVE`, `UNSUBSCRIBED`, `BOUNCED`; `invalid` adds `status = INVALID` + records whose `validation_status = INVALID`; `suppressed` = `suppression_status != CLEAR` |

Date windows filter `DeliveryJob.completed_at`, `NormalizedDeliveryEvent.event_time`, `Contact.created_at`, and `Campaign.created_at` (bare `YYYY-MM-DD` = midnight UTC, mirroring `campaign_analytics`).

## Service (`backend/app/services/reporting.py`)

`ReportingService(session, tenant_id)`:

- `contact_report(range, start_date, end_date)` → `{ total, active, unsubscribed, suppressed, invalid, inactive, bounced }`.
- `campaign_overview(...)` → `{ total, active, scheduled, completed, by_status }`.
- `delivery_overview(...)` → `{ sent, delivered, bounced, unsubscribed, complaints, temporary_failures, blocked, failed }`.
- `sender_overview()` → `{ total, connected, healthy, needs_attention }`.
- `campaign_report(...)` → per-campaign `{ campaign_id, campaign_name, status, recipients, sent, delivered, bounced, blocked, unsubscribed, complaints, failed, temporary_failures, delivery_rate }`.
- `sender_report(...)` → per-sender `{ sender_id, sender_name, sender_email, status, health_score, health, messages, successful, failed, temporary_failures, provider_throttling, blocked }`.
- `dashboard(...)` → combined `{ tenant_id, window, contacts, campaigns, delivery, senders }`.
- `export_csv(report, ...)` → RFC-4180-style CSV (quotes/commas escaped) for `campaigns`, `senders`, or `contacts`.
- `ReportingError` for invalid windows (raised before queries when `start > end`).

## API (`backend/app/api/reports.py`, gated by `analytics.read`)

All under `/api/v1`, registered in `app/main.py` as `reports_router`:

- `GET /reports/dashboard` — combined dashboard payload.
- `GET /reports/campaigns` / `GET /reports/senders` / `GET /reports/contacts` — per-report payloads.
- `GET /reports/export?report=campaigns|senders|contacts` — CSV `text/csv` response with `Content-Disposition: attachment`.

Shared query params: `range=today|7d|30d|90d|custom`, plus `start_date` / `end_date` (ISO) for `custom`. Invalid `range` or `report` → 422 (FastAPI validation); `ReportingError` → 400; unknown → 500.

## Frontend

- `types/reporting.ts` — `DashboardReport`, `ContactReport`, `CampaignOverview`, `DeliveryOverview`, `SenderOverview`, `CampaignReportRow`, `SenderReportRow`, `ReportFilter`.
- `api/reporting.ts` — `reportingApi.dashboard/campaigns/senders/contacts` plus `exportCsv` (fetches with the bearer token, downloads the blob as `{report}-report.csv`).
- `pages/Reports.tsx` — new **Reports & dashboard** page at `/reports` (nav item "Reports", `analytics.read` permission): a date-window filter (`All time / Today / Last 7 / 30 / 90 days / Custom`, with date inputs for custom), dashboard metric cards, a delivery KPI strip, per-campaign and per-sender tables, a contact breakdown, and CSV export buttons on each report panel. Queries are keyed on the filter so changing the window refetches. Styles added in `styles.css` (`.reports-page`, `.report-filters`, `.report-panel`, `.report-kpis`, `.report-table`, `.contact-breakdown`, …).

## Tests

- `backend/tests/test_reporting.py` (12 service tests) against a **known dataset**: exact contact/campaign/sender/delivery counts, per-campaign and per-sender rows with precise numbers, dashboard shape, cross-tenant isolation, the legacy `Message.status` fallback for tenants with no delivery queue, custom date-window filtering, and CSV content.
- `backend/tests/test_reports_api.py` (10 API tests): authed `TestClient` with an `analytics.read` role — dashboard/campaigns/senders/contacts endpoints, `range=today` and custom filters, 422 on invalid `range`/`report`, CSV `content-type` + header + row count, and unauthenticated rejection.

Backend suite fully green (403 passed); ruff and mypy clean on the new files; frontend `npm run build`, `npm run lint`, and `npm run test` (11 tests) all green.