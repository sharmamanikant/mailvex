# Docker Compose deployment

There is a single Compose definition, `docker-compose.yml`, used for the
production deployment on the Ubuntu host. There is no development override and
no separate production variant.

For the first-time install, including the host-level Cloudflare Tunnel, use
[ubuntu-cloudflare-tunnel.md](ubuntu-cloudflare-tunnel.md). This document is the
reference for the Compose file itself: services, variables, migrations, backups,
health, and upgrades.

## Topology

`cloudflared` runs on the host as a systemd service and forwards
`https://mailvex.in` to the nginx origin at `http://127.0.0.1:8080`. It is not a
Compose service.

| Service | Published port | Notes |
| --- | --- | --- |
| `nginx` | `127.0.0.1:8080` | Only published port. Loopback-only, so the app is unreachable from the LAN. |
| `backend` | none | API, non-root, waits for `migrate`. |
| `worker` | none | Celery worker. |
| `scheduler` | none | Celery beat. Exactly one. |
| `frontend` | none | Static SPA served by nginx. |
| `postgres` | none | `postgres_data` volume. |
| `redis` | none | `redis_data` volume, password required. |
| `migrate` | none | One-shot schema bootstrap/upgrade. Exits 0, then others start. |
| `backup` | none | Hourly dump to the `backups_data` volume. |

## Prerequisites

- Docker Engine 24+
- Docker Compose v2
- 2 GB RAM minimum, 4 GB recommended

## Every command needs the env file

Compose interpolates `RUNTIME_ENV_FILE` and the required secrets before it can
parse the file, so bare `docker compose` commands fail with an interpolation
error. Always pass the env file:

```bash
docker compose --env-file .env.production ps
```

Create it once:

```bash
cp .env.production.example .env.production
```

The template ships with `RUNTIME_ENV_FILE=.env.production` so the containers can
mount the same file they were configured from. Keep that line if you rename the
file, and set `RUNTIME_ENV_FILE` explicitly if you do.

## Environment variables

Required. Compose refuses to start if any is missing or still a placeholder:

- `RUNTIME_ENV_FILE`
- `POSTGRES_DB`, `POSTGRES_USER`, `POSTGRES_PASSWORD`
- `DATABASE_URL` — must embed the same password as `POSTGRES_PASSWORD`
- `REDIS_PASSWORD`
- `JWT_SECRET`, `ENCRYPTION_KEY`
- `ALLOWED_ORIGINS`, `ALLOWED_HOSTS`

`REDIS_URL` is **derived** in Compose as
`redis://:${REDIS_PASSWORD}@redis:6379/0` so the credential exists in exactly one
place. Do not set `REDIS_URL` in the env file; a mismatch there overrides the
derived value and produces a Redis authentication failure that surfaces as
`429` on every rate-limited route.

`ALLOWED_ORIGINS` also drives the API's CSP `connect-src`. Set it to real
origins only; a wildcard disables the protection.

Optional provider variables: `GOOGLE_CLIENT_ID`, `GOOGLE_CLIENT_SECRET`,
`MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET`, `MICROSOFT_TENANT_ID`,
`GOOGLE_REDIRECT_URI`, `MICROSOFT_REDIRECT_URI`.

Contact validation and import lifecycle variables (`VALIDATION_*`,
`VERIFICATION_CHUNK_SIZE`, `IMPORT_STALE_JOB_SECONDS`,
`IMPORT_RECLAIM_INTERVAL_SECONDS`) are documented in
[contact-validation.md §3](contact-validation.md#3-configuration).

## Database migration

Migrations run as the one-shot `migrate` service. `backend`, `worker`, and
`scheduler` declare `depends_on: migrate: service_completed_successfully`, so the
application cannot start against a stale schema.

```bash
docker compose --env-file .env.production run --rm migrate alembic current
```

### How a new database is created

The `migrate` service runs `python -m app.db_bootstrap`, not a bare
`alembic upgrade head`. The initial revision `20260823_01` builds the schema
from live model metadata rather than a frozen historical definition, so it
already materialises today's tables and the later revisions then collide with
them; replaying the chain against an empty database fails 28 times out of 43.

The bootstrap therefore branches:

| Database state                            | Action                                |
| ----------------------------------------- | ------------------------------------- |
| No tables at all                          | Create schema from models, stamp head |
| Has application tables                    | Ordinary `alembic upgrade head`       |
| Has tables but no `alembic_version` table | Refuse, exit non-zero                 |

Only the empty case is special, so existing databases keep the normal upgrade
path and new revisions apply normally after the initial stamp. A schema built
this way comes from the models, so review model/migration drift before relying
on it. The revision history itself is unchanged.

To apply pending migrations, `up -d` is enough. To run them explicitly:

```bash
docker compose --env-file .env.production run --rm migrate
```

Do not run `alembic upgrade head` inside the `backend` container. Two concurrent
upgrades against the same database is a worse failure mode than a failed deploy,
and the one-shot service exists precisely to make that impossible.

The application never auto-creates or auto-destroys production data. See
[production-release-gate.md](production-release-gate.md) for the
backup -> migrate -> verify -> start procedure.

## Startup and shutdown

```bash
docker compose --env-file .env.production build
docker compose --env-file .env.production up -d
docker compose --env-file .env.production ps
docker compose --env-file .env.production down
```

Never run `down -v` in production. It removes the named `postgres_data`,
`redis_data`, and `uploads_data` volumes.

## Verifying the origin

The origin URL is the exact target the tunnel forwards to:

```bash
curl -sS http://127.0.0.1:8080/health          # {"status":"ok"}
curl -sS http://127.0.0.1:8080/nginx-health    # ok
```

Confirm the real client IP is being preserved. The logged address must be a real
public IP, not `127.0.0.1` and not the Docker bridge gateway:

```bash
docker compose --env-file .env.production logs nginx --tail 20
```

## Backup and restore

For the full procedure see
[backup-disaster-recovery.md](../operations/backup-disaster-recovery.md).

The `backup` service runs hourly and writes to the `backups_data` volume (mounted
as `/backups` in the container): a gzipped `pg_dump`, the upload store, and a copy
of the runtime env file. Interval and retention come from
`BACKUP_INTERVAL_SECONDS`, `DB_RETENTION_DAYS`, `FILES_RETENTION_DAYS`, and
`CONFIG_RETENTION_DAYS`.

```bash
docker compose --env-file .env.production exec backup sh /scripts/backup.sh
docker compose --env-file .env.production exec backup ls -lh /backups
docker compose --env-file .env.production cp backup:/backups/crcrm-db-<stamp>.sql.gz ./
```

A named volume is used instead of a host bind mount because the container runs
as root; a bind mount would leave root-owned files on the host that the deploy
user cannot delete.

`/backups/config/<stamp>/runtime.env` contains `JWT_SECRET`, `ENCRYPTION_KEY`, and
the database and Redis passwords. Treat the backup volume as secret material:
never commit it, never serve it.

Manual dump:

```bash
docker compose --env-file .env.production exec postgres pg_dump -U app -d crcrm > backup.sql
docker compose --env-file .env.production exec -T postgres psql -U app -d crcrm < backup.sql
```

Redis is password-protected, so raw `redis-cli` needs credentials:

```bash
docker compose --env-file .env.production exec redis sh -c 'redis-cli -a "$REDIS_PASSWORD" SAVE'
```

## Logs

```bash
docker compose --env-file .env.production logs -f
docker compose --env-file .env.production logs -f backend worker scheduler nginx
```

## Health checks

| Endpoint | Purpose | Auth |
| --- | --- | --- |
| `/health` | Liveness | none |
| `/health/live` | Liveness | none |
| `/health/ready` | Readiness incl. DB, Redis, worker/scheduler heartbeat | none |
| `/health/details` | Detailed subsystem state | **admin** (401 otherwise) |
| `/nginx-health` | Edge-only probe served by nginx itself | none |

`/nginx-health` is answered by nginx without touching an upstream, so the nginx
healthcheck reports on the edge rather than its dependencies. A failing check
means the edge is broken, not that the backend is down.

## Background workers

`worker` (Celery) and `scheduler` (Celery beat) handle all asynchronous work. Run
**exactly one** scheduler; additional beat instances duplicate every periodic
task, including rule seeding and stale-import reclaim.

Beat entries include `dispatch-due-scheduled-messages`,
`dispatch-due-delivery-jobs`, `drive-warmup-schedule`, `sample-ops-metrics`,
`evaluate-ops-alerts`, `sweep-stale-import-files`, `safety-review-scan`,
`refresh-provider-credentials`, `seed-validation-rules`, and
`reclaim-stale-imports`.

## Upgrade procedure

1. Review configuration drift:
   ```bash
   git diff HEAD~1 -- .env.production nginx/ docker-compose.yml
   ```
2. Back up:
   ```bash
   docker compose --env-file .env.production exec backup sh /scripts/backup.sh
   ```
3. Rebuild and restart:
   ```bash
   docker compose --env-file .env.production build
   docker compose --env-file .env.production up -d
   ```
4. Verify:
   ```bash
   docker compose --env-file .env.production ps
   docker compose --env-file .env.production run --rm migrate alembic current
   curl -sS http://127.0.0.1:8080/health
   ```

Recreating `backend` or `frontend` does not require restarting `nginx`. The edge
resolves upstreams at request time via Docker's embedded DNS
(`resolver 127.0.0.11`), so a static `upstream` block would otherwise keep a
stale address and return `502` after any container recreate.

## Release gate

Before releasing, complete every gate in
[production-release-gate.md](production-release-gate.md).
