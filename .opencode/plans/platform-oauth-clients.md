# Plan: Super-admin OAuth client configuration from the UI

**Status:** READY — awaiting plan exit (plan mode is read-only; no commands run yet)
**Scope (user-confirmed):** Phase 0 full Docker clean + fresh rerun · Google **and** Microsoft OAuth ·
DB-first with env fallback · hosted on `/platform-admin` (SUPER_ADMIN only)

## Problem

`GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` / `MICROSOFT_*` are read once at import time
(`backend/app/core/config.py:328`, frozen dataclass `Settings`). Changing them requires an env
file edit + container recreate. The SaaS platform owner must be able to enter them at runtime
from the UI, without a deploy.

## Design

- New table `platform_oauth_clients`, one row per provider (`google` | `microsoft`).
- The row stores an opaque Fernet `credential_reference` produced by the existing
  `encrypt_credential_reference()` (`app/email_providers/credentials.py:75`) — payload
  `{"client_id", "client_secret", "tenant_id"}` (allowlist already accepts all three).
- Resolver: **DB first, env fallback, then "unset"**. No cache: it is a primary-key lookup on
  rare paths (consent URL, token refresh), so every uvicorn worker and Celery process sees
  writes immediately.
- Secret is **never** returned by any endpoint — only `client_id`, `tenant_id`,
  `secret_set`, `source` (`database|environment|unset`), and the configured redirect URIs.

## Backend

1. **Model + migration**
   - `PlatformOAuthClient` in `app/models/entities.py` (template: `ComplianceProfile:519`):
     `provider` (PK), `credential_reference`, `credential_version`, `updated_by`, `created_at`,
     `updated_at`. Export from `app/models/__init__.py`.
   - Alembic revision `20261008_01_platform_oauth_clients`, `down_revision = 20260925_08`.

2. **Resolver service** — new `app/services/platform_oauth_clients.py`
   - `resolve_client(provider) -> ResolvedClient(client_id, client_secret, tenant_id, source)`
     reads the row with `SessionLocal()` (`app/core/database.py:64`) when no session is passed,
     decrypts inside the provider boundary, falls back to `settings.google_client_*` /
     `settings.microsoft_client_*` / `settings.microsoft_tenant_id`.
   - `save_client(provider, payload, user_id)` (encrypts, upserts), `clear_client(provider)`.
   - Env values are used unchanged when the table has no row; empty env ⇒ `source="unset"`.

3. **API** — extend `app/api/platform_admin.py` (router already mounted at `app/main.py:302`),
   every endpoint `Depends(require_super_admin)` + `_audit()` (`platform_admin.py:38`):
   - `GET  /api/v1/platform-admin/oauth-clients` — both providers, incl. `redirect_uris`
     echoed from `settings.*_redirect_uri` so the admin knows exactly what to register.
   - `PUT  /api/v1/platform-admin/oauth-clients/{provider}` — `{client_id, client_secret?, tenant_id?}`;
     a blank `client_secret` keeps the stored one (rotation on write).
   - `DELETE /api/v1/platform-admin/oauth-clients/{provider}` — removes the row ⇒ env fallback again.

4. **Call-site swaps** — replace direct `settings.*_client_id/secret` reads with
   `resolve_client(...)`. Every site already builds its client config per request, so no
   lifecycle change is needed:

   | Google | Microsoft |
   | --- | --- |
   | `app/services/google_sender_oauth.py:75-86,107,134` | `app/services/microsoft_sender_oauth.py:76-78,258` |
   | `app/services/google_oauth.py:53-56,73,91` | `app/services/microsoft_oauth.py:60-98` |
   | `app/email_providers/google/workspace.py:86-102,174-175,205` | `app/email_providers/microsoft/workspace.py:115-117,460` |
   | `app/email_providers/google/provider.py:149-152,248-251,264` | `app/email_providers/microsoft/provider.py:140-145,331` |
   | `app/email_providers/mailbox_discovery.py:187-188` | |
   | `app/services/provider_connections.py:194-204` (shared) | |
   | `app/services/senders.py:324-348` (shared) | |

   - **Fixes a latent gap while we are there:** today `client_id` can come from a stored
     payload but `client_secret` is always env (`google/provider.py:150,249`,
     `google/workspace.py:175`, `mailbox_discovery.py:188`). After this change both come from
     the same resolved source.
   - Redirect URIs stay env-driven (`GOOGLE_*_REDIRECT_URI`, `MICROSOFT_*_REDIRECT_URI`);
     they are reported by the GET endpoint, not edited here.

5. **(Optional, included)** readiness surface: `services/readiness.py` gains
   `google_oauth` / `microsoft_oauth` = `configured|unconfigured`, shown by `/health/details`
   and the ops overview.

## Frontend

6. **API client** — `frontend/src/api/platformAdmin.ts`: `getOAuthClients`,
   `updateOAuthClient(provider, payload)`, `deleteOAuthClient(provider)`.

7. **UI** — new `admin-card` "OAuth clients" on `frontend/src/pages/PlatformAdmin.tsx`
   (page already nav-gated by `platform.admin` ⇢ `SUPER_ADMIN`, `Dashboard.tsx:18,35`;
   backend 403 is the real guard):
   - two forms (Google, Microsoft): Client ID, Client secret (password field, placeholder
     "•••• unchanged" when already stored), Microsoft tenant ID.
   - status badge per provider: **Saved via UI / Environment / Not configured**.
   - read-only list of the three redirect URIs per provider with a copy button.
   - Save + Clear actions via react-query mutation, invalidating the query on success.

## Local runtime fix (independent of the feature, needed for OAuth to work here)

8. `.env.local` currently has no `*_REDIRECT_URI` entries, so the defaults point at
   `http://localhost:8000/...`, which is **not published** — every OAuth callback would fail.
   Add `GOOGLE_REDIRECT_URI`, `GOOGLE_SENDER_REDIRECT_URI`, `GOOGLE_WORKSPACE_REDIRECT_URI`
   and the three `MICROSOFT_*` equivalents set to `http://localhost:8080/...` (nginx origin),
   then recreate backend/worker/scheduler.

## Phase 0 — Full clean, then fresh rerun (user-approved: "clean all rerun freshly")

**Irreversible:** wipes the local database, the `crcrm-local_backups_data` dumps, and every
leftover volume. `.env.local` (config) and the repo are untouched.

**0a. Teardown**
1. `docker compose -p crcrm-local --env-file .env.local down -v --remove-orphans` — stops and
   removes the 8 services **and** the named volumes `crcrm-local_{redis_data,uploads_data,backups_data}`.
2. `docker rm -f crcrm-test-redis crcrm-local-postgres crcrm-local-migrate-1` (any remainder).
3. `docker volume prune -f` — all unused volumes: 17 anonymous, old `crcrm_*` (4),
   `crcrm-local_postgres_data`, `crcrm-local_pgdata` (local DB recreated next run),
   `scraper_*` (2).
4. Images: `docker rmi crcrm-{backend,worker,scheduler,migrate,backup,frontend}`,
   `scraper-{backend,celery_worker,frontend,frontend-test}`, `kindest/node`, then
   `docker image prune -f`.
5. `docker network rm kind` (no containers attached); `crcrm-local_crcrm-net` goes with `down`.
6. **Keep** (unreferenced but origin unknown, user did not approve removal):
   `envoyproxy/envoy:v1.36.7`.
7. Keep: base images (`python:3.12-slim`, `alpine`, `redis:7-alpine`, `nginx:1.27-alpine`,
   `postgres:16-alpine`), Docker Desktop internals (`docker/desktop-*`).

**0b. Fresh run**
8. Start DB: recreate `crcrm-local-postgres` (same command as today: network alias `postgres`,
   volume `crcrm-local_pgdata`, init script creating `crcrm_backup` **with CREATEDB**).
9. `.env.local` gains the six redirect URIs on `http://localhost:8080/...` (see item 8 in
   Backend/Local fixes) **before** the rebuild so they bake into the new containers.
10. `docker compose -p crcrm-local --env-file .env.local build --no-cache` →
    `up -d` → `migrate` bootstraps the empty schema and stamps head.
11. **Verify:** `docker ps -a` = 9 up + `migrate` exited 0 · `curl :8080/health/ready` → 200 ·
    `:8080/` → 200 · worker logs show beat tasks succeeding · `docker exec crcrm-local-backup-1
    sh /scripts/backup.sh` completes with "Integrity check passed" (also proves the
    `crcrm_backup` CREATEDB grant).

**Post-clean state:** 9 running containers (8 compose + local postgres) + 1 exited one-shot —
no leftovers, all images freshly built.

## Tests

9. New `backend/tests/test_platform_oauth_clients.py` (guard pattern from
   `tests/test_auth_api.py:106-117`):
   - SUPER_ADMIN GET/PUT/DELETE → 200; non-super-admin → 403.
   - Response body never contains `client_secret`; `secret_set` flips true after PUT.
   - PUT with blank secret preserves the stored secret.
   - Resolver precedence: DB row beats env; env used when no row; `unset` when neither.
   - Existing Google/Microsoft OAuth tests keep passing (they stub `settings`).
10. Frontend: extend/加 existing PlatformAdmin test if present; otherwise rely on
    `tsc -b`, `eslint`, `vitest run` full gates.

## Verification

```bash
# backend (repo venv)
python -m pytest tests/test_platform_oauth_clients.py -q && python -m pytest -q
ruff check app tests ; mypy app          # baseline: 77 pre-existing errors
# frontend
npx tsc -b ; npx eslint . ; npx vitest run
# runtime
docker compose -p crcrm-local --env-file .env.local up -d --build backend worker scheduler
curl -s http://127.0.0.1:8080/health/ready
```
Manual: log in as SUPER_ADMIN → `/platform-admin` → save Google client ID/secret → badge
shows "Saved via UI" → confirm `/api/v1/platform-admin/oauth-clients` never leaks the secret.

## Risks / notes

- Migration is purely additive (new table) → safe on the running local DB and on production.
- No caching ⇒ a save takes effect immediately across the 2 uvicorn workers and Celery.
- Production startup still rejects `replace-*` placeholder env secrets
  (`config.py:217-226`); unchanged. Env becomes *optional* for these two providers only
  once a row exists.
- Docs to update: `docs/integrations/google.md` + `microsoft.md` "Configuration" sections
  (UI path now exists); env var lists in `docs/deployment/docker-compose.md` unchanged.

## Files touched

New: `backend/app/services/platform_oauth_clients.py`, migration
`backend/app/alembic/versions/20261008_01_platform_oauth_clients.py`,
`backend/tests/test_platform_oauth_clients.py`.
Modified: `entities.py`, `models/__init__.py`, `api/platform_admin.py`, the 11 call sites in
the table above, `services/readiness.py`, `frontend/src/api/platformAdmin.ts`,
`frontend/src/pages/PlatformAdmin.tsx`, `.env.local`, two integration docs.
