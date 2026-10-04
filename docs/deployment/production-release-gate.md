# Production Release Gate (Phase 25)

This document is the authoritative checklist for releasing **CR+CRM** to
production. A release is allowed **only** when every gate below reports
**PASS**; otherwise the release **must not** proceed. It covers environment
separation, configuration validation, the database migration strategy, Docker
image hardening, the reverse proxy, worker topology, monitoring, rollback, and
the final smoke test.

---

## 1. Environments

Four environments are supported and kept separated. Real credentials are
**never** committed; each environment uses its own private, git-ignored env
file.

| Environment | Env file (git-ignored) | Template (committed)            | App env values                          |
| ----------- | ---------------------- | ------------------------------- | --------------------------------------- |
| development | `.env`                 | [`.env.example`](../../.env.example) | relaxed, dev defaults, mock providers   |
| testing     | `.env.test`            | (reuse `.env.example`)          | pytest in-process (SQLite), no secrets  |
| staging     | `.env.staging`         | [`.env.staging.example`](../../.env.staging.example) | HTTPS, real-ish providers, dedicated DB/Redis |
| production  | `.env.production`      | [`.env.production.example`](../../.env.production.example) | HTTPS, real providers, must-set secrets |

**No production credentials exist in development.** `app/core/config.py` treats
both `production` and `staging` (`requires_explicit`) as environments that must
never fall back to development defaults:

- `DATABASE_URL`, `JWT_SECRET`, `ENCRYPTION_KEY`, `ALLOWED_ORIGINS`,
  `ALLOWED_HOSTS` raise at startup if unset in production/staging.
- `JWT_SECRET`/`ENCRYPTION_KEY` must be ≥ 32 bytes.
- `secure_cookies` and HSTS `includeSubDomains` are enabled for
  production/staging only.
- `initialize_database()` (schema create + dev seed) returns immediately for
  production/staging — development data is never seeded into those environments.

Deploy with the matching env file:

```bash
docker compose --env-file .env.staging up -d    # staging
docker compose --env-file .env.production up -d # production
```

---

## 2. Configuration validation

Validate every variable before release. Values are documented with defaults and
placeholders in:
[`.env.production.example`](../../.env.production.example).

| Variable | Requires real value    | Notes                                                     |
| -------- | ---------------------- | --------------------------------------------------------- |
| `DATABASE_URL` | Yes           | Postgres DSN (psycopg). Startup fails if unset.           |
| `REDIS_URL`    | Yes           | Redis DSN.                                                |
| `JWT_SECRET`   | Yes           | ≥ 32 bytes; startup fails if unset/weak.                  |
| `ENCRYPTION_KEY`| Yes          | ≥ 32 bytes; used for stored-provider-credential encryption. |
| `GOOGLE_CLIENT_ID/SECRET`, `MICROSOFT_CLIENT_ID/SECRET` | production client credentials | leave blank if provider disabled. |
| `GOOGLE/MICROSOFT_REDIRECT_URI` | Yes (HTTPS origin) | must match the OAuth app registration. |
| `AI_PROVIDER` + provider key | per provider | use `mock` unless metered AI is configured with a budget/limit. |
| `TRANSACTIONAL_SMTP_*` | Yes for email sends | secured relay only; `SMTP_ALLOW_PRIVATE/PLAINTEXT/INSECURE` stay `false` in prod. |
| `ALLOWED_ORIGINS` / `ALLOWED_HOSTS` | Yes | restrict to the real public origin + proxy host; startup fails if unset. |
| `PUBLIC_BASE_URL`, `PASSWORD_RESET_URL` | Yes | HTTPS URLs.                                               |
| `WEBHOOK_SECRETS` | Yes (per enabled provider) | `provider=<hmac-secret>` comma-separated; inbound delivery webhooks are rejected without it. |

Preflight (recommended in CI): with `APP_ENV=production`, run the backend once
and confirm startup does **not** raise, then hit `/health/ready`.

---

## 3. Database — production migration process

Production schema is managed **only** by forward Alembic migrations. The
application **never** auto-creates or auto-destroys production data
(`database.py` skips `create_all`/seeding for production/staging).

The `docker-compose.yml` `migrate` service runs migrations as an explicit
one-shot step that the API/worker/scheduler wait on
(`depends_on: condition: service_completed_successfully`).

Required procedure (in order):

1. **Backup** — ensure a verified dump exists before any schema change
   (see [backup-disaster-recovery.md](../operations/backup-disaster-recovery.md)).
2. **Migration** — run the dedicated migration step:
   ```bash
   docker compose --env-file .env.production run --rm migrate
   ```
3. **Verify** — confirm the applied revision is the expected head and the
   readiness probe reports `migrations: ready`:
   ```bash
   docker compose --env-file .env.production exec postgres psql -U app -d crcrm -c "select version_num from alembic_version"
   curl -fsS http://127.0.0.1:8000/health/ready
   ```
4. **Application startup** — only after verify passes, start (or upgrade) the
   application stack so the API/worker/scheduler attach to the migrated schema.

If any step fails, **stop** and follow the rollback strategy (section 8). Never
proceed to startup on a failed migration.

### 3.1 Revision drift check (required before migrating)

A recorded revision that lags the real schema causes `upgrade head` to attempt
re-creating objects that already exist. Confirm the recorded revision matches
the actual schema before running any migration:

```sql
SELECT version_num FROM alembic_version;
SELECT table_name FROM information_schema.tables
 WHERE table_schema = 'public'
   AND (table_name LIKE 'validation%' OR table_name = 'verification_jobs');
```

If objects exist that the recorded revision should not yet contain, **stop**.
Reconcile the recorded revision to match reality before migrating — do not
force the migration through.

### 3.2 Egress requirements

| Requirement | Needed by | Check |
| --- | --- | --- |
| Outbound DNS (UDP+TCP 53) | Contact validation MX/A lookups | `docker compose exec worker python -c "import dns.resolver as r; print(r.resolve('example.com','MX')[0])"` |
| Outbound TCP 25 (optional) | SMTP recipient probing only | Only if `VALIDATION_SMTP_ENABLED=true` |

Without DNS egress the validator reports `UNKNOWN` for every domain. This is
correct behaviour for a network that cannot resolve — it is not a bug, and it
must be detected at the gate rather than in production data.

---

## 4. Docker — production images

- Two production images are built by `docker-compose.yml`
  (`./backend/docker/Dockerfile`, `./frontend/docker/Dockerfile`).
- **No development servers / no debug mode**: backend uses `uvicorn`
  (production ASGI) with multiple workers; frontend serves the built `dist` via
  nginx. There is no Vite/dev server and no reload/debug in production images.
- **Non-root**: backend/worker/scheduler run as UID `10001` (image creates an
  `app` user, chowns the writable `/app/.local_uploads`). The `backup` sidecar
  is the deliberate exception (it writes the `backups_data` volume).
  Nginx workers already drop to the `nginx` user; see section 5 for edge-HTTPS
  notes.
- **Health checks**: every long-running service (`postgres`, `redis`, backend,
  worker, scheduler, frontend, nginx) declares a healthcheck; the API exposes
  `/health`, `/health/live`, `/health/ready`, `/health/details`.
- **Immutable, predictable images**: builds are deterministic from Dockerfiles;
  pinned base image tags are used (`python:3.12-slim`, `node:20-alpine`,
  `nginx:1.27-alpine`).

---

## 5. Reverse proxy / HTTPS

The edge nginx (`nginx/conf.d/default.conf`) is mounted read-only and provides:

- **HTTPS** (TLS 1.2/1.3) on `443`; HTTP on `80` redirects every public route
  to HTTPS. The Docker-only `/nginx-health` endpoint remains available on port
  80 for the container health check.
- **Security headers** on every response: HSTS (`includeSubDomains`),
  `X-Frame-Options: DENY`, `X-Content-Type-Options: nosniff`,
  `Referrer-Policy: no-referrer`, `Permissions-Policy`, `Content-Security-Policy`.
  The API emits its own headers on API responses, and the API's
  `content-security-policy` (built from `ALLOWED_ORIGINS`) takes precedence over
  the edge's. `ALLOWED_ORIGINS` must therefore contain only real production
  origins.
- **Upstream resolution**: `proxy_pass` uses a variable with
  `resolver 127.0.0.11`, so `backend`/`frontend` are resolved per request
  rather than cached at startup. Recreating a backend or frontend container
  therefore does not require restarting nginx. A persistent `502` indicates the
  resolver is unreachable (nginx running outside Docker, or a pre-change
  config).
- **Edge health probe**: `/nginx-health` is served by nginx itself, so the
  container healthcheck validates the edge independently of its upstreams.
- **Request limits / timeouts**: rate-limit zone (`20r/s`, burst 50) on `/api/`,
  request size limit (`client_max_body_size 20M`), and proxy
  read/connect timeouts.
- **Compression**: `gzip` for text/API/asset types.
- **Safe proxy headers**: `X-Real-IP`, `X-Forwarded-For`,
  `X-Forwarded-Proto`, `Host`, and HTTP/1.1 keepalive to upstreams.

### Certificates

There is no certificate to manage in the container. TLS terminates at
Cloudflare; the `cloudflared`-to-origin hop is loopback HTTP on
`127.0.0.1:8080`, and no certificate is mounted into nginx.

Gate on the tunnel instead:

- `cloudflared` runs as a host systemd service, not a Compose service:
  `systemctl is-active cloudflared` must return `active`.
- Cloudflare SSL/TLS mode is **Full (strict)**, and **Always Use HTTPS** is on.
- `https://mailvex.in` presents a certificate Cloudflare issues and renews.
  The origin never needs one.
- DNS for `mailvex.in` is a `CNAME` to `<TUNNEL-ID>.cfargotunnel.com`, not an
  `A` record pointing at the private IP.
- After changing `nginx/snippets/cloudflare-realip.conf` or the edge config:
  `docker compose --env-file .env.production exec nginx nginx -s reload`.

---

## 6. Workers — deployed separately

API, worker, and scheduler are **separate services** with **independent
restart** policies in `docker-compose.yml`:

| Service | Role                          | Restart | Wait-on                  |
| ------- | ----------------------------- | ------- | ------------------------ |
| `migrate` | one-shot Alembic upgrade    | `no`    | postgres/redis healthy   |
| `backend` | HTTP API (uvicorn, `${UVICORN_WORKERS:-2}` workers) | always | migrate completed + deps healthy |
| `worker`  | Celery worker (`${CELERY_WORKER_CONCURRENCY:-2}`) | always | migrate completed + deps healthy |
| `scheduler` | Celery beat               | always  | migrate completed + deps healthy |

Each can be restarted, scaled, or drained independently without affecting the
others. Readiness confirms worker/scheduler via Redis heartbeats
(`/health/ready`).

---

## 7. Monitoring

Existing in-house observability is hardened and consumed at release (no
additional containers required):

- **Logs** — structured single-line JSON on stdout (correlation
  `request_id`/`tenant_id`/`user_id`, with credential redaction) via
  `docker compose logs -f`.
- **Metrics** — Redis-backed counters / gauges / latency buckets exposed through
  the ops API (`GET /api/v1/ops/metrics`) and OpsCenter UI.
- **Alerts** — `AlertService` rules (DB/Redis/worker/scheduler availability,
  queue growth, provider failure rate, webhook verification failures, critical
  senders, disk space) reconciled to durable `AlertRecord`s; surfaced in the ops
  API and UI.
- **Health checks** — `/health`, `/health/live`, `/health/ready` (unauthenticated)
  and `/health/details` (admin-only), plus per-container Docker healthchecks
  and the nginx-only `/nginx-health`.

Operators should consume `docker compose logs`, poll `/health/ready`, and
forward `/api/v1/ops/*` to their paging/notification of choice.

---

## 8. Rollback

### 8.1 Application rollback

Redeploy the previous image tag and re-run the previous Compose definition:

```bash
# Revert backend (and worker/scheduler/frontend) to a known-good build tag
git checkout <previous-release-tag> docker-compose.yml
docker compose --env-file .env.production up -d
```

The API has no persistent in-process state, so an application rollback is a pure
code/container swap; Redis is ephemeral and rebuilds on demand.

### 8.2 Database migration rollback

Migrations are forward-only versioned migrations. To roll back a **failed or
unwanted** migration step:

1. **Restore from backup** (there is always a verified pre-change dump):
   ```bash
   docker compose --env-file .env.production \
     exec backup sh /scripts/restore.sh /backups/crcrm-db-<pre-change>".sql.gz
   ```
2. Or, for a reversible step, run the downgrade for that single revision:
   ```bash
   docker compose --env-file .env.production \
     run --rm migrate alembic downgrade <previous-revision>
   ```
   Downgrades are only used when the step is safely reversible and data was
   preserved by the migration author; otherwise **restore, never downgrade**.

### 8.3 Configuration rollback

Keep the previous env file. To revert: replace the current env file with the
previous one and restart:
`docker compose --env-file .env.production up -d`.
Configuration is not stored in the DB, so no data change is required.

---

## 9. Final smoke test

Run the automated smoke test against the deployment
(see [smoke-test.md](../operations/smoke-test.md)):

```bash
cd backend
python tests/smoke_test.py --base https://<public-origin>/api  # or http://<host>:8000
```

Covered flows: **login, contact creation, contact import, template creation,
AI draft, sender endpoint, campaign creation, compliance, schedule, campaign
analytics, analytics dashboard, audit log, delivery event (bounce), and
unsubscribe/suppression**.

Manual / provider-bound flows requiring a configured OAuth + SMTP sender at
release time: **real sender connection**, **test send**, and end-to-end
**delivery** of a scheduled campaign.

---

## 10. Release gate checklist

A release is allowed **only** if **all** gates are **PASS**.

| Gate                    | Check                                                                                                                                 | Status |
| ----------------------- | -------------------------------------------------------------------------------------------------------------------------------------- | ------ |
| Security                | Phase 23 audit critical/high fixed; runtime secrets not committed; secrets ≥ 32 bytes; admin-only `/ops/*`. | PASS (Phase 23) |
| Tests                   | Backend suite green; `ruff` clean; `mypy` no new errors. Run on host/CI, not inside the production container.                            | PASS |
| Integration             | `/health/ready` reports all subsystems ready against the deployed env.                                                                  | PASS |
| Database                | `migrate` completes; `alembic_version` == expected head; no auto-destroy path in prod; no revision drift (§3.1).                        | PASS |
| Redis                   | `/health/ready` redis + worker + scheduler heartbeat ready. Exactly one scheduler.                                                      | PASS |
| Docker                  | Production images build; non-root app; health checks present; `docker compose config` valid.                                            | PASS |
| Edge proxy              | `/nginx-health` 200; HTTPS serves API + SPA; `/health/details` admin-protected; TLS not self-signed.                                    | PASS |
| DNS egress              | Worker resolves MX/A records (§3.2). Required by contact validation.                                                                    | VERIFY AT RELEASE |
| Contact validation      | `VALIDATION_DEFAULT_PHONE_REGION` set to the operating region; `VALIDATION_SMTP_ENABLED` set deliberately ([contact-validation.md](contact-validation.md)). | VERIFY AT RELEASE |
| Monitoring              | Logs structured; metrics/readiness reachable; alert rules evaluate.                                                                     | PASS |
| Backup                  | Verified dump produced by the backup sidecar (`backup-disaster-recovery.md`).                                                           | VERIFY AT RELEASE |
| Restore test            | Restore exercised into a scratch DB per `restore-test-results.md`.                                                                      | VERIFY AT RELEASE |
| Sender integration      | OAuth connect + test email + campaign delivery succeed with a configured HEALTY sender.                                                 | VERIFY AT RELEASE |
| Compliance              | Campaign `validate` returns PASS/BLOCK correctly; delivery events honored.                                                              | PASS |

> ⚠️ Gates marked **VERIFY AT RELEASE** depend on live external accounts
> (real backup volume, real provider senders, restore exercise, real DNS
> egress) that cannot be fully executed in a developer stack. They **must** be
> confirmed in the target environment before the release proceeds. Otherwise:
> **DO NOT RELEASE.**
