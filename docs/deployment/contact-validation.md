# Contact Validation & Import Reliability — Deployment and Operations

Deployment runbook for the contact validation engine, Celery-backed imports,
and migration `20260925_08`.

- Gate compliance: [production-release-gate.md](production-release-gate.md)
- Stack operations: [docker-compose.md](docker-compose.md)
- Backup/restore: [../operations/backup-disaster-recovery.md](../operations/backup-disaster-recovery.md)
- Post-deploy checks: [../operations/smoke-test.md](../operations/smoke-test.md)

---

## 1. Release contents

| Area | Change | Requires action |
| --- | --- | --- |
| Schema | 3 tables, 17 `contacts` columns, 8 indexes | Migration step |
| Imports | Celery-backed, heartbeat, stale-job reclaim | Worker must reach Postgres + Redis |
| Imports | Successful imports auto-enqueue verification | None |
| Workers | 4 new tasks, 2 new beat entries | **Exactly one** scheduler |
| Config | 19 new variables, all defaulted | See [§3](#3-configuration) |
| Edge | nginx resolves upstreams at request time | Reload nginx |
| Dependency | `phonenumbers>=8.13,<9` | Rebuild images |

No new service, queue, or database. Uses the existing Postgres, Redis, Celery
and nginx.

### Verdict semantics

`verification_status` and `risk_level` are **advisory**. Campaign selection does
not read them, and no contact is auto-excluded on score alone.

- Verdicts: `VALID`, `LIKELY_VALID`, `NEEDS_REVIEW`, `RISKY`, `INVALID`, `UNKNOWN`
- `VERIFIED` is written only after an SMTP `RCPT TO` returns `250`. Without SMTP
  probing the ceiling is `LIKELY_VALID`.
- Free-mailbox providers (Gmail, Outlook, Yahoo, iCloud) are `FREE_MAILBOX` at
  `LOW` risk and are not penalised.
- Role accounts (`info@`, `support@`) set `role_account=true` and are not invalid.
- Disposable domains yield `email_status=INVALID` with an overall verdict of
  `RISKY` / `HIGH`. To exclude them, filter `disposable = true`.

---

## 2. Pre-flight

### 2.1 Required

- [ ] **Outbound DNS (UDP + TCP 53)** from the worker network. Validation
      resolves MX and A records; without egress every domain returns `UNKNOWN`
      and the feature is inert.
      ```bash
      docker compose --env-file .env.production exec worker python -c "import dns.resolver as r; print(r.resolve('example.com','MX')[0])"
      ```
- [ ] **No local TLS to configure.** TLS terminates at Cloudflare and the
      `cloudflared`-to-origin hop is loopback HTTP on `127.0.0.1:8080`. There is
      no certificate in the container and none to replace.
- [ ] **Secrets** in `.env.production`: `DATABASE_URL`, `JWT_SECRET`,
      `ENCRYPTION_KEY`, `POSTGRES_PASSWORD`, `REDIS_PASSWORD`, `ALLOWED_ORIGINS`,
      `ALLOWED_HOSTS`, `PUBLIC_BASE_URL`. `JWT_SECRET` and `ENCRYPTION_KEY` must
      be ≥ 32 bytes; startup fails otherwise. Set `REDIS_PASSWORD` and let
      Compose derive `REDIS_URL` from it.
- [ ] **`ALLOWED_ORIGINS` set to real production origins only.** It also drives
      the API's CSP `connect-src` (see [§7.5](#7-known-issues)).
- [ ] **Verified backup** taken immediately before migrating.
- [ ] **`VALIDATION_DEFAULT_PHONE_REGION`** set to the operating region. The
      code default is `IN`; `.env.production.example` ships `US`.

### 2.2 Optional — SMTP probing

Disabled by default. Enable only if all of the following hold:

- [ ] Outbound TCP **25** permitted from the worker network.
- [ ] The sending IP has PTR and is not blocklisted.
- [ ] You accept the reputation sensitivity of recipient probing.

With SMTP disabled or blocked, the engine degrades honestly:
`smtp_status` becomes `TIMEOUT` or `NOT_PROBED` and MX-level validation still
works. Nothing is fabricated.

### 2.3 Alembic state check

A drifted `alembic_version` is the highest-risk failure mode for this
migration. The migration is idempotency-guarded and will not double-apply, but
if the recorded revision is **behind** the real schema, `upgrade head` will
attempt to re-create existing objects.

```sql
SELECT version_num FROM alembic_version;
SELECT table_name FROM information_schema.tables
 WHERE table_schema = 'public'
   AND (table_name LIKE 'validation%' OR table_name = 'verification_jobs');
```

If the objects exist but `version_num` is older than `20260916_07`, **stop**.
Do not run `upgrade head`. Reconcile the recorded revision first.

---

## 3. Configuration

Defaults ship in `.env.production.example`; the values below are that file's.
All are optional — the engine is safe with defaults.

### Import lifecycle

| Variable | Default | Notes |
| --- | --- | --- |
| `IMPORT_STALE_JOB_SECONDS` | `300` | Must exceed the slowest import batch, or live imports are reclaimed and restarted mid-run. |
| `IMPORT_RECLAIM_INTERVAL_SECONDS` | `60` | Beat cadence for `crcrm.reclaim_stale_imports`. |

### Validation

| Variable | Default | Notes |
| --- | --- | --- |
| `VALIDATION_SMTP_ENABLED` | `false` | See [§2.2](#22-optional--smtp-probing). |
| `VALIDATION_SMTP_TIMEOUT_SECONDS` | `5.0` | Per-probe timeout. |
| `VALIDATION_SMTP_MAX_PROBES_PER_RUN` | `200` | Ceiling per verification run. |
| `VALIDATION_SMTP_CIRCUIT_BREAKER_THRESHOLD` | `10` | Consecutive failures before short-circuiting a host. |
| `VALIDATION_SMTP_CIRCUIT_BREAKER_COOLDOWN_SECONDS` | `900` | Cooldown before retrying. |
| `VALIDATION_MAX_UNIQUE_DNS_LOOKUPS` | `500` | Per-run cap on unique domains. Contacts past the cap are recorded `UNKNOWN`, never a pass. |
| `VALIDATION_DNS_TIMEOUT_SECONDS` | `2.0` | Per-query resolver timeout. |
| `VALIDATION_DOMAIN_CACHE_TTL_SECONDS` | `21600` | MX/A cache. |
| `VALIDATION_EMAIL_CACHE_TTL_SECONDS` | `604800` | Per-address syntax/provider cache. |
| `VALIDATION_PHONE_CACHE_TTL_SECONDS` | `604800` | Per-number cache. |
| `VALIDATION_SCORE_VERIFIED_MIN` | `90` | Unreachable while SMTP is disabled. |
| `VALIDATION_SCORE_LIKELY_VALID_MIN` | `70` | |
| `VALIDATION_SCORE_NEEDS_REVIEW_MIN` | `45` | |
| `VALIDATION_DUPLICATE_DEFINITE_MIN` | `80` | |
| `VALIDATION_DUPLICATE_POSSIBLE_MIN` | `55` | |
| `VERIFICATION_CHUNK_SIZE` | `200` | Contacts per Celery task slice. Lower if the worker OOMs. |
| `VALIDATION_DEFAULT_PHONE_REGION` | `IN` | Set to the operating region. |

Environment variable names map to attributes with different prefixes, so verify
resolved values rather than assuming:

```bash
docker compose --env-file .env.production exec worker python -c \
  "from app.core.config import settings as s; \
   print(s.verification_chunk_size, s.validation_default_phone_region, s.import_stale_job_seconds)"
```

---

## 4. Schema

Migration `20260925_08_contact_validation` (`down_revision` `20260916_07`).

**New tables**

- `verification_jobs` — job state, counters, `scope`, `filters`,
  `request_payload`, `celery_task_id`
- `validation_domain_rules` — global domain dispositions
- `validation_local_rules` — global local-part rules

**New `contacts` columns** (all `NOT NULL` with defaults)

`verification_status`, `verification_score`, `verification_details`,
`risk_level`, `email_status`, `email_type`, `email_provider`, `domain_status`,
`mx_status`, `smtp_status`, `role_account`, `disposable`, `duplicate_status`,
`duplicate_score`, `phone_status`, `phone_type`, `last_verified_at`

Existing rows backfill to `verification_status='UNKNOWN'`,
`risk_level='UNKNOWN'`, `smtp_status='NOT_PROBED'`. No existing contact is
marked invalid.

**New indexes**

- `ix_contacts_tenant_verification_status`, `ix_contacts_tenant_verification_score`
- `ix_contacts_tenant_risk_level`, `ix_contacts_tenant_last_verified`
- `ix_verification_jobs_tenant_status`, `ix_verification_jobs_tenant_created`
- `ix_validation_domain_rules_domain`, `ix_validation_local_rules_local_part`

**Locking.** `ADD COLUMN` with a constant default is catalog-only on
PostgreSQL 11+, so no table rewrite occurs, but `ACCESS EXCLUSIVE` is still
required and blocks reads and writes for the duration. On a large `contacts`
table, migrate in a maintenance window.

**Rule seeding.** `crcrm.seed_validation_rules` runs hourly, is idempotent, and
returns `{'inserted': 0}` on subsequent runs. It is global reference data
shared by all tenants and needs no per-tenant seed. Force it immediately with:

```bash
docker compose --env-file .env.production exec backend python -c \
  "from app.tasks.scheduler import seed_validation_rules; print(seed_validation_rules())"
```

---

## 5. Deployment

### 5.1 Production

`docker-compose.yml` runs migrations as a one-shot `migrate` service and
gates `backend`, `worker` and `scheduler` behind
`service_completed_successfully`, so the application never starts against a
stale schema.

```bash
# 0. Confirm the env file exists and sets RUNTIME_ENV_FILE (required by Compose).
test -f .env.production && grep '^RUNTIME_ENV_FILE=' .env.production

# 1. Back up.
docker compose --env-file .env.production exec backup sh /scripts/backup.sh

# 2. Review configuration drift.
git diff HEAD~1 -- .env.production nginx/conf.d/default.conf docker-compose.yml

# 3. Build images.
docker compose --env-file .env.production build

# 4. Start. The migrate service must exit 0.
docker compose --env-file .env.production up -d

# 5. Confirm the revision.
docker compose --env-file .env.production exec backend alembic current
# expect: 20260925_08 (head)
```

If `migrate` exits non-zero, dependent services do not start. Read its logs,
correct, re-run `up -d`.

```bash
docker compose --env-file .env.production logs migrate
```

### 5.2 Development / single-host

```bash
docker compose --env-file .env.production build
docker compose --env-file .env.production up -d redis backup
docker compose --env-file .env.production run --rm migrate
docker compose --env-file .env.production up -d
```

---

## 6. Post-deploy verification

```bash
# 1. All services healthy.
docker compose --env-file .env.production ps

# 2. Schema at head.
docker compose --env-file .env.production exec backend alembic current
docker compose --env-file .env.production exec backup sh -c 'psql -h "$PGHOST" -U "$PGUSER" -d "$PGDATABASE" -c "\d verification_jobs"'

# 3. Backfill is UNKNOWN, never INVALID.
docker compose --env-file .env.production exec backup sh -c 'psql -h "$PGHOST" -U "$PGUSER" -d "$PGDATABASE" -c "select verification_status, count(*) from contacts group by 1;"'

# 4. Celery tasks registered (18 expected).
docker compose --env-file .env.production exec backend python -c \
  "from app.tasks.scheduler import celery_app; print(len([t for t in celery_app.tasks if t.startswith('crcrm.')]))"

# 5. Rulebook seeded (33 domain / 29 local on a clean database).
docker compose --env-file .env.production exec backup sh -c 'psql -h "$PGHOST" -U "$PGUSER" -d "$PGDATABASE" -c "select category, count(*) from validation_domain_rules group by 1 order by 1;"'

# 6. Edge reachable.
curl -fsS https://<host>/health
curl -fsS https://<host>/nginx-health
```

Then run [../operations/smoke-test.md](../operations/smoke-test.md) and the
validation-specific flow below.

### Validation flow

1. Import a CSV of 10–50 rows containing a free-mailbox address, a role
   address, and a disposable domain.
2. Confirm the import reaches `COMPLETED` and a verification job is created.
3. Poll `GET /api/v1/contacts/verification-jobs/{id}` until `COMPLETED`.
4. Confirm the expected dispositions:

| Input | Expected |
| --- | --- |
| `someone@gmail.com` | `LIKELY_VALID`, `LOW`, `email_type=FREE_MAILBOX` |
| `info@company.com` | `LIKELY_VALID`, `LOW`, `role_account=true` |
| `x@mailinator.com` | `RISKY`, `HIGH`, `email_status=INVALID` |
| `a@gmial.com` | `RISKY`, `HIGH`, `mx_status=MISSING` |

If every row shows `mx_status=UNKNOWN`, stop and resolve DNS egress
([§2.1](#21-required)) — the engine is behaving correctly, the environment is not.

> Run the test suite on the host or in CI, not inside the production container.
> `TrustedHostMiddleware` rejects the test client's `Host: testserver` header
> because the container's `ALLOWED_HOSTS` holds only real hostnames, so
> in-container runs fail with `400 Invalid host header` regardless of code health.

---

## 7. Operations

### Monitoring

- Job state: `GET /api/v1/contacts/verification-jobs/{id}`
- Aggregate: `GET /api/v1/contacts/verification-summary`
- Stalled imports: `ImportJob` rows in an active state whose `updated_at` is
  older than `IMPORT_STALE_JOB_SECONDS`
- Worker health: `/health/ready` and the ops API (`/api/v1/ops/metrics`)

### Capacity

| Constraint | Value |
| --- | --- |
| Import file size | 20 MB (`client_max_body_size`). Split larger CSVs. |
| Import file retention | `IMPORT_FILE_RETENTION_DAYS` (default 30), swept by `crcrm.sweep_stale_import_files`. Uploaded CSVs persist in the `uploads_data` volume and are captured by the backup sidecar. |
| API rate limit | 20 req/s, burst 50 per IP on `/api/`. The Contacts page polls the active job every 3s — budget for concurrent users. |
| DNS budget | `VALIDATION_MAX_UNIQUE_DNS_LOOKUPS` per run; excess becomes `UNKNOWN`. |
| Worker memory | 512 MB. Raise it, or lower `VERIFICATION_CHUNK_SIZE`, before large bulk runs. |
| Worker concurrency | `CELERY_WORKER_CONCURRENCY` (default 2). Each slot holds a chunk in memory. |
| Schedulers | **Exactly one.** Multiple beat schedulers duplicate periodic work. |

Run the first production bulk verification outside peak hours and watch worker
memory and job duration before scheduling it routinely.

### Rollback

The schema change is additive, so the previous application version runs
unchanged against the migrated database. **Prefer application rollback.**

```bash
git checkout <previous-tag> docker-compose.yml
docker compose --env-file .env.production up -d
```

To halt in-flight verification without rolling back:

```bash
docker compose --env-file .env.production stop worker
```

Schema downgrade **destroys all recorded verdicts** — use only when the previous
application version is genuinely incompatible, and restore from backup rather
than downgrading whenever data is at stake:

```bash
docker compose --env-file .env.production exec backend alembic downgrade 20260916_07
```

### Known issues

1. **Disposable domains verify as `RISKY`, not `INVALID`.** The domain resolves
   with valid MX, so the overall verdict stays `RISKY` / `HIGH` even though
   `email_status=INVALID`. Filter `disposable = true` to exclude them rather
   than changing score thresholds.
2. **`created_after` is a time bound, not a batch key.** Post-import
   verification selects `created_at >= job.started_at`, so a contact created by
   another flow during the import window is included. For exact batch
   membership, set `verification_jobs.filters` explicitly.
3. **Import quota is enforced per chunk.** Headroom is checked as each batch
   commits, so an import failing on quota mid-run can leave partial rows
   committed with the job marked `FAILED`. Read the report before retrying; a
   re-run is idempotent.
4. **nginx upstream resolution.** `proxy_pass` uses a variable plus
   `resolver 127.0.0.11`, so `backend`/`frontend` are resolved per request.
   Running nginx outside Docker, or with a config predating this change, breaks
   the resolver. A persistent `502` from nginx means the upstream address is
   unresolved — reload nginx.
5. **CSP `connect-src` derives from `ALLOWED_ORIGINS`.** `app/main.py` sets
   `content-security-policy` on every API response, and that header takes
   precedence over the one nginx adds. Any development origin left in
   `ALLOWED_ORIGINS` is emitted into the live CSP, and a wildcard entry disables
   the protection. The nginx-side CSP (`connect-src 'self'`) governs
   nginx-served static assets.
