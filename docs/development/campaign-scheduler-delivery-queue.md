# Campaign Scheduler + Delivery Queue (Phase 15)

Phase 15 replaced the campaign "schedule" path with a production-grade durable
delivery queue built around the new `delivery_jobs` table. The legacy
`ScheduledMessage` scheduler remains in place, deprecated and untouched, so the
existing `tests/test_scheduler.py` flow stays green.

## Delivery queue (`DeliveryJob`)

`DeliveryJobService` (`app/services/delivery_jobs.py`) is the core service. The
`delivery_jobs` table stores one row per `(tenant_id, campaign_id, recipient_id)`
(unique constraint `uq_delivery_job_recipient`) so materialization is naturally
idempotent.

- Statuses: `PENDING`, `PROCESSING`, `SENT`, `DELIVERED`, `FAILED`, `BLOCKED`,
  `CANCELLED`.
  - Working (claimable/claim-relevant): `PENDING`, `PROCESSING`.
  - Terminal: `SENT`, `DELIVERED`, `FAILED`, `BLOCKED`, `CANCELLED`.
- Job constants, transient failure codes, and retry timing:
  - `TRANSIENT_FAILURE_CODES` — `PROVIDER_TIMEOUT`, `PROVIDER_UNAVAILABLE`,
    `NETWORK_ERROR`, `TEMPORARY_REJECTION`, `SMTP_TRANSIENT`.
  - Exponential backoff `RETRY_BASE_SECONDS = 60`, capped at
    `RETRY_MAX_SECONDS = 3600`.
  - `max_attempts` defaults to 5.

### Concurrency-safe claiming

`claim` performs a single atomic `UPDATE ... WHERE ... RETURNING` with
`synchronize_session=False` (avoids ORM stale-write hazards), setting a lease
(`lease_owner`, `lease_until`) and transitioning the job to `PROCESSING`.
Lease-expired jobs (crashed workers) are re-claimable on the next discovery
pass — a job is only skipped if it is currently leased AND its lease has not
expired. Only one worker ever owns a job; the unique per-recipient constraint
and the lease guard together prevent duplicate sends.

### Retry, backoff, and throttle

- Transient failures (`mark_failure`, `retryable=True`) → status `FAILED` with
  `next_attempt_at` computed for exponential backoff; the job is dispatched again
  once due and `attempt_count < max_attempts`, otherwise it becomes terminal.
- Permanent failures (`retryable=False`) → `BLOCKED` (terminal) immediately.
- `throttle` handles `SenderThrottledError` from the provider: status `FAILED`,
  `failure_code = PROVIDER_THROTTLED`, `next_attempt_at = now + retry_after`
  (honoring the provider-supplied `Retry-After`).

### Discovery loop

`crcrm.dispatch_due_delivery_jobs` runs on a 15-second Celery beat and uses the
indexed `ix_delivery_jobs_discovery (status, next_attempt_at)` query to find due
working jobs (skipping `PAUSED`/`CANCELLED` campaigns), enqueuing
`crcrm.deliver_job(tenant_id, job_id)` per job. The worker task claims the job
and runs `DeliveryJobWorker.process` (`app/workers/delivery_jobs.py`), then calls
`mark_completed_if_done` when a job reaches a terminal state, flipping the
campaign to `COMPLETED` when all jobs are terminal.

## Sending integration

`send_delivery_job` (`app/services/sending.py`) mirrors the legacy
`send_scheduled` path but against a job row:

- Requires the job to be `PROCESSING`.
- Guards a `CANCELLED` campaign → jobs transition to `CANCELLED`, returns `None`.
- Runs the compliance gate; a `BLOCK` marks the job `BLOCKED` and raises
  `SendBlockedError` (worker records it as terminal, no resend).
- Renders the approved campaign version, enforces per-sender capacity limits,
  calls the provider send, creates the `Message`, and updates campaign statistics.

### Suppression (two gates)

Suppression is enforced at two points:

1. **During scheduling** — `materialize_for_campaign` skips any recipient that is
   actively suppressed (`SuppressionEngine.is_suppressed`), so no delivery job is
   created for an already-suppressed recipient.
2. **Immediately before sending** — the compliance gate in `send_delivery_job`
   (`check_recipient` → `evaluate`, including the `suppression` check) remains the
   authoritative backstop. A recipient suppressed after scheduling gets the job
   marked `BLOCKED` (terminal, non-retryable), never sent.

## API

`app/api/scheduler.py` was rewired to `DeliveryJobService`:

- `POST /campaigns/{id}/schedule` — materialize `delivery_jobs` (idempotent);
  campaign must be `APPROVED`, sender `CONNECTED`/`HEALTH_WARNING`.
- `POST /campaigns/{id}/send-now` — flush pending jobs to `next_attempt_at = now`
  and enqueue worker tasks immediately.
- `POST /campaigns/{id}/pause` / `resume` — toggles discovery; in-flight jobs may
  finish.
- `POST /campaigns/{id}/cancel` — cancels pending jobs (default reason
  "Campaign cancelled") and sets the campaign to `CANCELLED`.
- `GET /campaigns/{id}/delivery-progress` — per-status counts for all job
  statuses plus the campaign status and total, used by the frontend progress UI.

## Frontend

`Campaigns.tsx` renders a "Delivery progress" panel in the campaign detail view:

- `campaigns.ts` API client gains `deliveryProgress`.
- A `useQuery` fetches progress and `refetchInterval = 5000ms` while the campaign
  is `SCHEDULED`, `RUNNING`, or `PAUSED`, stopping once terminal.
- A progress bar (`DeliveryProgressBar`) shows the terminal-fraction complete and
  the sent fraction, plus per-status count chips (`PENDING` ... `CANCELLED`).

## Migration

`app/alembic/versions/20260830_23_delivery_jobs.py` (head, revises
`20260829_22`) creates `delivery_jobs` with all columns, foreign keys, the unique
per-recipient constraint, and the two discovery/tenant indexes. `readiness`
`EXPECTED_MIGRATION` was bumped to `20260830_23`.

## Validation

New coverage in `tests/test_delivery_jobs.py` (26 tests): one-per-recipient
materialization and idempotency, DRAFT rejection, suppression-skip during
scheduling, claim-once-only, expired-lease recovery, terminal-skip claiming,
transient retry backoff, permanent `BLOCKED`, max-attempts terminal, throttle
backoff, pause stops new sends (in-flight continues), cancel→campaign cancelled,
resume, progress counts, discovery only-due + paused excluded, tenant isolation,
worker success/blocked/deferred/failed handling, duplicate-send prevention, and
suppression-blocked-immediately-before-sending.

Full backend suite: **330 passing** (304 prior + 26 new). Ruff clean; mypy clean
for all new Phase 15 files (only the pre-existing strict-mode baseline errors
remain). Frontend `npm run lint`, `npm run build`, and `npm test` all pass.
Deployed containers healthy; `/health/details` all `ready`; scheduler beat is
dispatching `dispatch-due-delivery-jobs` on 15s and the worker registers both
`crcrm.deliver_job` and `crcrm.dispatch_due_delivery_jobs`.
