# Production Deployment Runbook

This runbook deploys the CR+CRM Compose stack to one production host. It is
written for the repository state containing the contact-validation release
(`20260925_08_contact_validation`). Complete the blockers in the companion
[production readiness assessment](production-readiness-assessment.md) before
using this procedure for a public launch.

Related material:

- [Production release gate](production-release-gate.md) -- mandatory go/no-go checklist
- [Contact-validation release notes](contact-validation-release.md) -- feature-specific checks
- [Backup and disaster recovery](../operations/backup-disaster-recovery.md) -- recovery contract
- [Smoke test](../operations/smoke-test.md) -- post-release functional check

## 1. Deployment model

The production Compose definition starts the following services on the private
`crcrm-net` bridge network:

| Service | Purpose | Exposure |
| --- | --- | --- |
| PostgreSQL | Application database on the private database host | private network, allowlisted from the app host |
| `redis` | Celery broker, cache, rate-limit and operational state | private only |
| `migrate` | one-shot Alembic migration job | private only |
| `backend` | FastAPI/Uvicorn API | through nginx only |
| `worker` | Celery asynchronous work | private only |
| `scheduler` | single Celery Beat scheduler | private only |
| `frontend` | compiled React application | through nginx only |
| `nginx` | TLS edge proxy | host ports 80 and 443 |
| `backup` | PostgreSQL backup sidecar | `backups_data` volume |

Do not run a second `scheduler`; duplicate Beat instances can enqueue periodic
work twice. Scale `worker` only after confirming that the worker tasks and
broker configuration support the desired level of parallelism.

## 2. Host prerequisites

- A supported Linux host with Docker Engine 24+ and Docker Compose v2.
- A Cloudflare account with the public hostname's zone, and `cloudflared`
  installed on the host as a systemd service. DNS for the hostname is a
  `CNAME` to `<TUNNEL-ID>.cfargotunnel.com`.
- No local certificate. TLS terminates at Cloudflare; the tunnel forwards to
  loopback HTTP on `127.0.0.1:8080`.
- **No inbound firewall rule is required.** `cloudflared` establishes an
  outbound connection, so the host needs no open inbound port at all. If you
  also want LAN access, allow it explicitly; it is not a deployment
  prerequisite. PostgreSQL, Redis, backend, and frontend publish no port.
- Outbound access from the backend/worker for the enabled OAuth providers,
  SMTP relay, and DNS resolver. Contact validation requires UDP and TCP 53.
- Persistent, encrypted storage for the external database, uploads, and
  backups. Compose uses its `uploads_data` named volume for application upload
  storage; protect the Docker volume with encrypted host storage.
- Off-host replication for backups. The `backups_data` volume alone does not
  protect against loss of the host.

## 3. Prepare release inputs

Work from an immutable, reviewed release commit or tag. Record its Git SHA in
the change ticket. Keep the current and immediately previous release source,
image identifiers, and encrypted environment file available for rollback.

Create the production environment file outside source control:

```powershell
Copy-Item .env.production.example .env.production
```

The template includes `RUNTIME_ENV_FILE=.env.production`. Keep this value
aligned with the file passed to `--env-file`; it prevents production services
from accidentally inheriting the root development `.env`.

Set the values for the actual public hostname and production accounts. At a
minimum, configure distinct, non-placeholder values for:

- `POSTGRES_HOST`, `POSTGRES_DB`, `POSTGRES_USER`, and `POSTGRES_PASSWORD`;
  `DATABASE_URL` must use the same app-role credentials
- `BACKUP_PGUSER` and `BACKUP_PGPASSWORD` for a dedicated backup role with
  database read access and `CREATEDB` for restore verification
- `JWT_SECRET` and `ENCRYPTION_KEY` (32 bytes or more)
- `REDIS_URL`
- `ALLOWED_ORIGINS` and `ALLOWED_HOSTS`
- `PUBLIC_BASE_URL` and `PASSWORD_RESET_URL` (HTTPS)
- enabled OAuth client credentials and their exact registered callback URLs
- transactional SMTP credentials if the platform sends account email
- `WEBHOOK_SECRETS` for every enabled inbound webhook provider
- `VALIDATION_DEFAULT_PHONE_REGION` and validation capacity settings

Treat the environment file as a secret. Never send it in tickets, logs, or
chat. Confirm that it is ignored by Git:

```powershell
git check-ignore -v .env.production
```

Validate the rendered service configuration without printing secrets:

```powershell
docker compose --env-file .env.production config --quiet
```

## 4. Release procedure

1. Announce the maintenance window and stop new deployments. For a migration
   with meaningful locking risk, pause campaign launches/imports first.
2. Confirm the pre-release gates, especially the latest successful backup and
   a restore test. Do not proceed on an unverified backup.
3. Build the release artifacts and retain the prior artifact identifiers:

   ```powershell
   docker compose --env-file .env.production build
   ```

4. Start Redis and the backup service. Confirm the external PostgreSQL host is
   reachable from the app host on port 5432:

   ```powershell
   docker compose --env-file .env.production up -d redis backup
   docker compose --env-file .env.production ps
   docker compose --env-file .env.production exec backup pg_isready -h 192.168.0.12 -p 5432 -U crcrm_app -d crcrm
   ```

5. Produce a fresh verified database backup immediately before migration:

   ```powershell
   docker compose --env-file .env.production exec backup sh /scripts/backup.sh
   ```

   Record the generated dump name and verify the command completed with exit
   code zero. The sidecar snapshots the shared upload volume and a read-only
   copy of the runtime env file. The host backup directory must be encrypted,
   access-controlled, and replicated off-host.

6. Run Alembic as the dedicated one-shot service:

   ```powershell
   docker compose --env-file .env.production run --rm migrate
   docker compose --env-file .env.production run --rm migrate alembic current
   ```

   The returned revision must be the repository's expected Alembic head. If it
   is not, stop; do not start the application services. The `migrate` container
   connects to the external PostgreSQL host using `DATABASE_URL`.

7. Start the application services:

   ```powershell
   docker compose --env-file .env.production up -d --remove-orphans backend worker scheduler frontend nginx
   docker compose --env-file .env.production ps
   ```

8. Verify local and public readiness:

   ```powershell
   curl.exe -fsS https://<public-host>/health/ready
   curl.exe -fsSI https://<public-host>/
   docker compose --env-file .env.production logs --tail 100 backend worker scheduler nginx
   ```

   Expect `200` from readiness, healthy long-running containers, an HTTPS
   certificate for the intended hostname, and no repeating connection or
   migration errors. `/health/details` requires an authenticated user with
   `settings.manage` and should not be used as an unauthenticated monitor.

9. Run the automated smoke test plus the provider-bound manual checks in
   [smoke-test.md](../operations/smoke-test.md). Verify one OAuth connection,
   a transactional email, one sender test, and one controlled campaign delivery
   with an approved production sender.

10. Monitor worker queue depth, failed jobs, delivery events, backup logs, and
    error rate during the first operating window. Record the release SHA,
    image identifiers, migration revision, backup artifact, and test evidence.

## 5. Routine operations

Useful commands (always supply the same production env file):

```powershell
docker compose --env-file .env.production ps
docker compose --env-file .env.production logs -f backend
docker compose --env-file .env.production logs -f worker
docker compose --env-file .env.production restart worker
```

Restarting the worker is preferable to restarting the entire stack for a
worker-only fault. Restarting `scheduler` is safe only when there remains
exactly one scheduler instance.

### 5.1 Redis capacity

Redis holds the Celery broker (queued send jobs), the Celery result backend,
rate-limit counters, warmup advisory locks and sender daily quotas. It is
capped **below** its container memory limit and uses `noeviction`:

| Variable           | Default | Meaning                                             |
| ------------------ | ------- | --------------------------------------------------- |
| `REDIS_MAXMEMORY`  | `192mb` | Redis starts returning OOM errors at this point      |
| `REDIS_MEMORY_LIMIT` | `256M` | Hard container limit (cgroup)                        |

Keeping `maxmemory` under the container limit is deliberate: Redis begins
failing predictably while it still has headroom to serve reads, instead of being
OOM-killed by the kernel and restart-looping.

`noeviction` is not a tuning preference. Evicting a key means losing a queued
send job or resetting a quota counter, and in both cases the campaign still
reports as queued while nothing is delivered. Instead, a full Redis surfaces
`503` from the routes that depend on it (rate limiting, OAuth state, sender
quotas, contact import).

Check headroom and alert before it matters:

```bash
docker compose --env-file .env.production exec redis \
  redis-cli -a "$REDIS_PASSWORD" --no-auth-warning info memory | grep -E 'used_memory_human|maxmemory_human'
docker compose --env-file .env.production exec redis \
  redis-cli -a "$REDIS_PASSWORD" --no-auth-warning info stats | grep rejected_connections
```

If Redis is repeatedly at `maxmemory`, raise `REDIS_MAXMEMORY` together with
`REDIS_MEMORY_LIMIT` and the host's available RAM. Do not switch the policy to
an eviction strategy to make the symptom disappear.

### 5.2 Restoring from backup

The restore path uses a dedicated `restore` profile service, not the running
`backup` sidecar. The sidecar mounts the filestore read-only so a backup job can
never mutate live files; only `restore` mounts it read-write.

```bash
docker compose --env-file .env.production stop backend worker scheduler
docker compose --profile restore --env-file .env.production \
  run --rm restore /backups/crcrm-db-<stamp>.sql.gz
docker compose --env-file .env.production up -d backend worker scheduler frontend nginx
```

Full procedure: [Backup and disaster recovery](../operations/backup-disaster-recovery.md).

## 6. Rollback and incident boundaries

For an application-only failure, redeploy the prior reviewed release and
re-run the post-deploy readiness checks. Do not assume a code rollback makes a
database migration safe to reverse.

For migration/data failure, stop writers, preserve logs and the failing state,
then restore the verified pre-change backup according to
[Backup and disaster recovery](../operations/backup-disaster-recovery.md).
Use an Alembic downgrade only when the specific migration was reviewed as
reversible and no data loss is possible. Otherwise restore the database.

Never run `docker compose down -v` in production: it removes named database
and Redis volumes.
