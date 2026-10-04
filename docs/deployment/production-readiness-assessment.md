# Production Readiness Assessment

Assessment date: 2026-09-28  
Scope: repository configuration and source review only. No production host,
provider account, certificate, or backup restore was executed.

## Decision

**Conditional hold.** The P0 configuration gaps identified in the initial
review have been corrected in the Compose/nginx files. A public launch remains
blocked until the attached production evidence proves that the changes work:
an upload restore, secure configuration recovery, and an external HTTP-to-HTTPS
test. The release gate cannot be marked PASS from source review alone.

## What is already in place

| Area | Evidence | Assessment |
| --- | --- | --- |
| Service separation | `docker-compose.yml` separates API, worker, scheduler, migration, database, Redis, frontend, nginx and backup roles. | Good single-host baseline. |
| Database migrations | A one-shot `migrate` service gates API/worker/scheduler via Compose health/completion conditions. Production/staging skip automatic schema creation in `backend/app/core/database.py`. | Correct deployment pattern; validate against the real database before release. |
| Runtime hardening | Backend/worker/scheduler run as UID/GID 10001; production config rejects missing/placeholder/short key secrets. | Good. |
| Edge controls | TLS server block, HSTS, CSP/security headers, request-size limit, nginx health endpoint and API rate limiting are present. | Good after HTTP policy is corrected and trusted certificates are installed. |
| Operational probes | `/health`, `/health/live`, `/health/ready`, Docker health checks, structured logging and an Ops API exist. | Good; attach real monitoring and alert routing. |
| Recovery tooling | Scripts create and restore PostgreSQL dumps; the dump script restores each dump into a scratch database for integrity validation. | Database path is promising, but incomplete for the complete application state. |
| Contact validation | DNS/SMTP behavior is explicitly bounded and defaults safely; dedicated release instructions exist. | Configure DNS egress and capacity before enabling. |

`docker compose --env-file .env.production.example
config --quiet` completed successfully on 2026-09-28. Docker emitted a local
client-config permission warning, but Compose configuration validation passed.

## P0 fixes requiring production verification

| Finding | Evidence | Required resolution and proof |
| --- | --- | --- |
| Upload storage is now a shared named volume. | `uploads_data` is mounted at `STORAGE_ROOT` in backend, worker, scheduler and read-only in backup. | Upload an import file and error report, run backup, replace the application containers, then restore to a clean stack and compare content/access. |
| Configuration capture is now explicit. | The runtime env file is mounted read-only in backup as `/run/config/runtime.env`; `CONFIG_FILE` points to it. `RUNTIME_ENV_FILE` is set by production/staging templates. | Store the `backups_data` volume on encrypted, access-controlled storage; restore the configuration using the approved secret-management process without exposing it in logs. |
| HTTP now redirects public requests to HTTPS. | The port-80 nginx server retains only Docker's exact `/nginx-health` response; all other routes return HTTPS redirect. | From an external network, confirm HTTP `/`, `/api/...`, and `/health` redirect and HTTPS serves the correct trusted certificate. |

## Important release requirements (P1)

| Finding | Impact | Recommended action |
| --- | --- | --- |
| Images are built on the production host from mutable dependency ranges. | A rebuild can select different Python dependencies; Compose has no registry image digests or release image tags. | Build in CI, scan/sign images, publish immutable tags/digests, and have Compose deploy those images. Record the digest in the release record. |
| The production guide calls the image builds deterministic. | That claim is not supported by `requirements.txt` ranges and unpinned base-image digests. | Amend release documentation/process to describe the build accurately until CI artifact pinning exists. |
| Backup artifacts remain in the `backups_data` volume unless an external process copies them. | Host loss defeats the stated disaster-recovery target. | Encrypt and replicate to independent object storage/region; test restore using only replicated artifacts. |
| Certificates and provider flows are unverified in the target environment. | OAuth redirects, delivery, and TLS may fail despite local configuration. | Register exact HTTPS callback URLs, install a trusted certificate, and perform controlled OAuth/send/webhook tests. |
| Worker capacity has not been measured. | Imports, validation and mail delivery can exhaust memory or queues. | Load-test realistic campaigns/imports, set worker concurrency and verification chunk size from measurements, and alert on queue growth. |

## Pre-launch verification record

Attach the following evidence to the change/release ticket. A blank item is a
failed gate, not an implicit approval.

- [ ] Release SHA and immutable image digest recorded.
- [ ] Production env validation succeeded; no placeholder values remain.
- [ ] DNS, TLS certificate, firewall and public hostname verified.
- [ ] Latest pre-migration backup has a successful scratch restore.
- [ ] Upload and configuration recovery tested after the P0 storage fix.
- [ ] Alembic revision is the expected head before and after deployment.
- [ ] `/health/ready` returns 200 with database, Redis, worker and scheduler ready.
- [ ] Automated smoke suite passed against the public HTTPS endpoint.
- [ ] OAuth callback, webhook signature, transactional mail, sender test and a controlled delivery passed.
- [ ] Monitoring/paging receives a deliberate health or worker failure signal.
- [ ] Backup replication and restore from off-host media passed.

## Explicit non-findings

This assessment is not a penetration test, legal/compliance certification,
performance benchmark, cloud-infrastructure review, or a claim that external
OAuth/SMTP/AI vendors are configured. Those require access to the intended
production environment and accounts.
