# Phase 10A — Clerk Authentication Migration + Email Provider Foundation

**Completed:** 2026-09-05
**Status:** Backend + frontend implemented; all gates green.

## 1. Files changed

Backend (new):
- `backend/app/identity/__init__.py`, `schemas.py`, `clerk.py`, `dependencies.py`, `mapping.py`
- `backend/app/api/providers.py`, `backend/app/api/sender_connections.py`
- `backend/app/alembic/versions/20260905_29_clerk_identity.py`
- `backend/tests/test_clerk_identity.py`, `backend/tests/test_spec_alias_endpoints.py`

Backend (modified):
- `backend/app/api/integrations.py` — bulk sender import + enable/disable endpoints
- `backend/app/services/integrations.py` — `import_senders` (savepoint-per-row), `set_sender_status`
- `backend/app/schemas/integrations.py` — `SenderImport*`, `SenderAccount*`, credential schemas
- `backend/app/core/config.py` — `CLERK_*`, `AUTH_LEGACY_ENABLED`
- `backend/app/core/database.py` — `WORKSPACE_PERMISSION_KEYS`
- `backend/app/security/permissions.py` — dual-path `get_current_principal`, OWNER bypass
- `backend/app/api/auth.py` — `_require_legacy_auth()` gate
- `backend/app/models/entities.py` — `external_identity_id`, `identity_provider`
- `backend/app/services/auth.py` — permission bundle wiring
- `backend/app/main.py` — mount `/providers`, `/sender-connections` routers
- `backend/tests/test_database_metadata.py` — allowlist update

Frontend (new):
- `frontend/src/pages/SignIn.tsx`, `SignUp.tsx`, `SignIn.test.tsx`, `SignUp.test.tsx`
- `frontend/src/pages/Integrations.tsx`, `frontend/src/api/integrations.ts`, `frontend/src/types/integrations.ts`
- `frontend/src/vite-env.d.ts`

Frontend (modified/replaced):
- `frontend/src/main.tsx` (explicit Clerk publishable key)
- `frontend/src/App.tsx` (Clerk session driver + protected routes)
- `frontend/src/api/client.ts` (Clerk-compatible `me`)
- `frontend/src/pages/Dashboard.tsx` (Integrations nav + OWNER permission)
- `frontend/src/pages/Senders.tsx` (System B sender-account page)
- `frontend/src/styles.css` (integration/provider/senders styles)

Frontend (deleted): `Login.tsx`, `Signup.tsx`, `Login.test.tsx`

Env/docs:
- `.env.example`, `.env.production.example`, `.env.staging.example`
- `docs/development/clerk-migration.md`, `docs/development/phase-10a-clerk-auth-report.md`

## 2. Migrations

- `20260905_29_clerk_identity` (head, down_revision `20260903_28`): adds
  `users.external_identity_id` (unique index) and `users.identity_provider`
  (server default `CLERK`). Verified single head via `alembic heads`.

## 3. Clerk verification architecture

- `app/identity/clerk.py`: RS256 JWT verification via cached JWKS (kid-driven
  refresh), issuer/audience checks, email/display-name claim extraction.
- `app/identity/mapping.py`: resolves → links (single email match, audit
  `CLERK_IDENTITY_LINKED`) → provisions (new tenant + OWNER + permission bundle;
  commits; IntegrityError race repair; SECRET-mail placeholders).
- `app/security/permissions.py`: Clerk-first, gated legacy fallback; bypass set
  `{ADMIN, SUPER_ADMIN, OWNER}`.
- Frontend: `@clerk/react` at root, `App.tsx` drives the session (`isLoaded` →
  `isSignedIn` → `getToken()` → `/auth/me`), prebuilt `SignIn`/`SignUp` pages,
  `UserButton` in the shell.

## 4. Legacy migration status

- Legacy endpoints (signup/login/refresh/password-reset) gated behind
  `AUTH_LEGACY_ENABLED` (default `true`), so existing tenants are unaffected.
- `GET /auth/me` serves both identities and is how the Clerk session loads the
  app user/tenant.
- Controlled retirement: flip `AUTH_LEGACY_ENABLED=false` after tenants link.
  Legacy login pages removed from the frontend; `/login`/`/signup` redirect to
  `/sign-in`/`/sign-up`.

## 5. Email provider foundation (System B)

- `app/email_providers/base.py` + Google/Microsoft/SMTP providers with capability
  metadata; all network operations (send, inbox, webhooks, credential refresh)
  raise `ProviderMethodNotImplemented` in this phase.
- Connection lifecycle (create/store credentials/validate/disconnect/delete),
  sender discovery (local simulation only), and sender-account registration with
  per-row savepoint bulk import.

## 6. Credential & secret security

- Secrets accepted once via `CredentialUpload`, encrypted at rest, never
  returned; responses expose only `credential_configured` + version.
- Provider responses and audit trail strip secret material.
- No secrets committed; env templates contain placeholders only.

## 7. API endpoints

- `GET /api/v1/providers` (catalog alias)
- `GET|POST /api/v1/sender-connections`, `GET .../{id}`,
  `POST .../{id}/validate|disconnect|discover`, `DELETE .../{id}`
- `POST /api/v1/integrations/{id}/credentials|refresh-credentials`
- `GET .../senders`, `POST .../senders`, `PATCH /integrations/senders/{id}`
- `POST /integrations/senders/import`, `POST /integrations/senders/{id}/enable|disable`
- All under `integrations.*` permissions; tenant-scoped.

## 8. UI pages

- `/sign-in`, `/sign-up` (Clerk), Dashboard shell with `UserButton`, protected
  routes.
- `/integrations`: provider cards + Connect Wizard + connection management.
- `/senders`: System B sender-account table with enable/disable/validate/delete.

## 9. Test results

- Backend: `pytest` full suite **510 passed**; `ruff check app tests` clean;
  mypy clean on all new/changed app code (baseline ~44 pre-existing errors in
  untouched files only).
- Frontend: `tsc -b` clean, `eslint` clean, `vitest run` **22 passed**,
  `vite build` succeeded.

## 10. Remaining production blockers

1. **Clerk production instance + domain/proxy config** before go-live.
2. **Set `CLERK_AUDIENCE`** to the production audience (blank currently accepts any).
3. **Legacy tenants must link** their Clerk identity before
   `AUTH_LEGACY_ENABLED=false`.
4. **Real provider integrations** (OAuth flows, sending, webhooks, inbox sync)
   land in a later phase — current provider is local simulation only.
5. **Sender OAuth callbacks** and credential refresh are not yet wired to real
   provider APIs.
6. **Frontend bundle** exceeds the 500 kB advisory (code-splitting TODO).
7. `vite-env.d.ts` added for `import.meta.env` typing; confirm CI tsc config.
8. Rollout requires running migration `20260905_29` on prod/staging DBs.