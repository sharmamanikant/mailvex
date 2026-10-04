# Campaign Analytics (Phase 16)

Per-campaign analytics derived from **durable provider events**, exposed as an API and surfaced on the campaign detail page. Unlike early ad-hoc dashboards, these numbers come from normalized delivery events and the per-recipient delivery queue — no manual counters.

## Design principles

- **Event-first outcomes.** Deliveries, bounces, complaints and unsubscribes come from `NormalizedDeliveryEvent` rows (`DELIVERED`, `TEMPORARY_FAILURE`, `HARD_BOUNCE`, `COMPLAINT`, `UNSUBSCRIBED`). These are de-duped at the DB level on `(tenant_id, provider, provider_event_id)`, so re-delivered provider events never double-count.
- **Queue-based send path.** Send-path metrics (queued/processing/sent/blocked/failed) come from `DeliveryJob.status` buckets. If a campaign predates the delivery queue (no `delivery_jobs` rows), it falls back to legacy `Message.status` counts so older campaigns are never undercounted.
- **No fabricated metrics.** Engagement numbers we cannot observe from a source of truth are omitted (e.g. open/click rates are not invented) rather than guessed.

## Endpoints (all require `analytics.read`)

Registered under `/api/v1` in `app/main.py`.

### `GET /api/v1/campaigns/{campaign_id}/analytics`
Top metrics plus percentage deltas:

```jsonc
{
  "campaign_id": "…",
  "campaign_name": "…",
  "status": "RUNNING",
  "metrics": {
    "recipients": 500, "queued": 0, "processing": 0, "sent": 500,
    "delivered": 470, "temporary_failures": 12, "hard_bounces": 9,
    "unsubscribes": 4, "complaints": 1, "blocked": 2, "failed": 5
  },
  "percentages": {
    "sent": 100.0, "delivered": 94.0, "bounced": 1.8,
    "unsubscribed": 0.8, "complained": 0.2, "blocked": 0.4, "failed": 1.0
  }
}
```

### `GET /api/v1/campaigns/{campaign_id}/analytics/timeline`
Chronological campaign lifecycle events (`Created`, `Scheduled`, `Running`, `Completed`, approval, pause/resume/cancel). Built from `AuditLog` actions and delivery-job markers.

### `GET /api/v1/campaigns/{campaign_id}/analytics/recipients`
Paginated per-recipient status. Query params: `page` (≥1), `page_size` (1–200, default 50), optional `status`, `start_date`, `end_date`.

```jsonc
{ "campaign_id": "…", "count": 25, "total": 500, "page": 1, "page_size": 25,
  "items": [{ "name": "…", "email": "…", "status": "delivered",
              "status_code": "2.0.0", "timestamp": "…", "reason": null }] }
```

### `GET /api/v1/campaigns/{campaign_id}/analytics/export`
Streams `campaign-{campaign_id}-results.csv` with columns `recipient,status,event,timestamp,reason`.

### `GET /api/v1/campaigns/reports/delivery`
Rollup report across all campaigns: `{ "campaign_report": [...], "sender_report": [...] }`.

## Implementation

- `backend/app/services/campaign_analytics.py` — `CampaignAnalyticsService` (`metrics`, `summary`, `timeline`, `recipient_activity`, `export_csv`, `campaign_report`, `sender_report`).
- `backend/app/api/campaign_analytics.py` — router registered under `/campaigns`; all routes gated by `require_permission("analytics.read")`.
- `backend/app/services/delivery_jobs.py` — suppression skip at scheduling time plus audit writes at schedule/running/completed transitions (feeds the timeline).

## Tests

- `backend/tests/test_campaign_analytics.py` — 11 unit tests: metrics from jobs/events, duplicate events (no double count), out-of-order events, missing events, percentages, tenant isolation, pagination, status/date filters, CSV export, timeline.
- `backend/tests/test_campaign_analytics_api.py` — 5 API tests covering the summary, recipients pagination, CSV export, 404 for unknown campaigns, and auth guarding.

Backend suite: 346 passing. Frontend: `npm run build`, `npm run lint`, `npm run test` all green.
