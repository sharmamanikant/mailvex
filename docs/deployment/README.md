# Production Deployment

Deployment documentation for CR+CRM. Start here.

| Document | Use when |
| --- | --- |
| [ubuntu-cloudflare-tunnel.md](ubuntu-cloudflare-tunnel.md) | **Deploying to Ubuntu behind Cloudflare Tunnel.** The authoritative install guide: architecture, `cloudflared` as a host service, DNS, verification, troubleshooting. |
| [production-release-gate.md](production-release-gate.md) | **Releasing.** The authoritative gate checklist. A release proceeds only when every gate is PASS. |
| [docker-compose.md](docker-compose.md) | Operating the stack day to day: services, env vars, migrations, backups, health, upgrades. |
| [contact-validation.md](contact-validation.md) | Deploying or operating contact validation, bulk verification, or Celery-backed imports. |
| [../operations/backup-disaster-recovery.md](../operations/backup-disaster-recovery.md) | Backup, restore, and disaster recovery. |
| [../operations/smoke-test.md](../operations/smoke-test.md) | Verifying a deployment, automated and manual. |
| [../operations/restore-test-results.md](../operations/restore-test-results.md) | Evidence of a restore exercise. |

## Topology

There is a single `docker-compose.yml`. Ingress is a Cloudflare Tunnel whose
`cloudflared` process runs as a **systemd service on the host**, not as a
container, and forwards to the loopback-only nginx origin at
`http://127.0.0.1:8080`. Postgres and Redis publish no port. Earlier
`docker-compose.prod.yml`, `docker-compose.cloudflared.yml`,
`docker-compose.tunnel.yml`, and `docker-compose.cloudflared.overlay.yml`
variants were removed; their only content was either duplicated into the single
file or described running the tunnel in Docker, which is not the chosen design.

Full rationale and install steps: [ubuntu-cloudflare-tunnel.md](ubuntu-cloudflare-tunnel.md).


## Deploying an environment

```bash
cp .env.production.example .env.production     # then replace every placeholder
docker compose --env-file .env.production build
docker compose --env-file .env.production up -d
```

`RUNTIME_ENV_FILE` is a **required** variable: Compose refuses to parse the file
without it, and the env file supplies it for itself. Keep the
`RUNTIME_ENV_FILE=.env.production` line in the copied file so the stack
self-resolves; set it explicitly when the file is named differently.

`migrate` runs as a one-shot service and must exit 0; `backend`, `worker` and
`scheduler` wait on it, so the application never starts against a stale schema.

Uploaded files (import CSVs, error reports) live in the `uploads_data` volume,
shared by `backend`, `worker`, `scheduler` and `backup`, so they survive a
container recreate and are included in backups.

## Deploying a change to an existing environment

1. Review configuration drift: `git diff HEAD~1 -- .env.production nginx/ docker-compose.yml`
2. Back up: `docker compose --env-file .env.production exec backup sh /scripts/backup.sh`
3. Build and start: `docker compose --env-file .env.production up -d --build`
4. Verify: `alembic current`, `docker compose --env-file .env.production ps`, `curl http://127.0.0.1:8080/health`, `/nginx-health`
5. Smoke test: `python backend/tests/smoke_test.py --base https://<host>/api`

Full gate: [production-release-gate.md](production-release-gate.md).

## Environment files

| Environment | Env file | Template |
| --- | --- | --- |
| development | `.env` | [.env.example](../../.env.example) |
| testing | `.env.test` | reuse `.env.example` |
| staging | `.env.staging` | [.env.staging.example](../../.env.staging.example) |
| production | `.env.production` | [.env.production.example](../../.env.production.example) |

Real credentials are never committed. `app/core/config.py` refuses to start in
`production`/`staging` with placeholder or under-length secrets.

## Invariants worth knowing before you deploy

- **Schema is additive and forward-only.** Application rollback is a pure
  container swap; only revert the schema when the previous application version
  genuinely cannot run against it, and prefer restoring a backup over
  downgrading.
- **Exactly one Celery beat scheduler.** Additional schedulers duplicate every
  periodic task.
- **`ALLOWED_ORIGINS` is also the CSP allowlist.** A wildcard or leftover
  development origin weakens the live Content-Security-Policy.
- **Contact validation needs outbound DNS.** Without it every domain reports
  `UNKNOWN` — correct behaviour, but useless data. See
  [contact-validation.md](contact-validation.md).
- **Recreating containers does not require restarting nginx.** The edge
  resolves upstreams per request via Docker's embedded DNS.
- **Run tests on the host or in CI**, not inside the production container;
  `TrustedHostMiddleware` rejects the test client's `Host` header there.
