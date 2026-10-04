# Phase 10B — Google Sender OAuth + Test Send (Report)

**Completed:** 2026-09-07
**Status:** Backend + frontend implemented; all gates green.

The Google integration (System B) is production-capable for sending: real Gmail
adapter, end-to-end OAuth authorization-code flow, encrypted token storage,
identity-bound re-auth, duplicate protection, and a live test-send round trip.

---

## 1. Files changed

Backend (new):
- `backend/app/services/oauth_state_store.py` — Redis single-use state store
- `backend/app/services/google_sender_oauth.py` — OAuth flow service
- `backend/app/api/google_sender_oauth.py` — authorize/callback/test router
- `backend/app/alembic/versions/20260906_01_google_sender_connection_unique.py`
- `backend/tests/test_google_oauth_security.py`

Backend (modified):
- `backend/app/email_providers/google/provider.py` — real validate/send + typed refresh
- `backend/app/email_providers/base.py` — `ProviderErrorCode` enum + `EmailMessage`
- `backend/app/email_providers/credentials.py` — allow `token_uri` payload field
- `backend/app/services/integrations.py` — REAUTH mapping, GOOGLE_* audits,
  `send_test_email`, duplicate-create guard
- `backend/app/schemas/integrations.py` — `GoogleAuthorize*`, `SenderTest*` schemas
- `backend/app/api/integrations.py` / `sender_connections.py` — 409 conflict mapping
- `backend/app/core/config.py` — `GOOGLE_SENDER_REDIRECT_URI`
- `backend/app/models/entities.py` — partial unique index
- `backend/app/main.py` — mount `/senders` router + rate limit
- `backend/tests/test_email_provider_foundation.py`,
  `tests/test_integrations.py`, `tests/test_spec_alias_endpoints.py`

Frontend (new):
- `frontend/src/pages/GoogleIntegration.tsx`

Frontend (modified):
- `frontend/src/App.tsx` — `/integrations/google` route
- `frontend/src/pages/Integrations.tsx` — GOOGLE card → OAuth page, Reconnect action
- `frontend/src/pages/Senders.tsx` — per-sender Test button
- `frontend/src/api/integrations.ts` — `googleOAuthApi.authorize`, `sendTestEmail`
- `frontend/src/types/integrations.ts` — `GoogleAuthorize*`, `TestSend*` types

Docs:
- `docs/integrations/google.md`

## 2. Real Google adapter

`app/email_providers/google/provider.py` (existing System B adapter, filled in):
- `connect`/`validate` → `users().getProfile` against a live Google
  `Credentials` object, identity re-check against bound `google_sub`/email.
- `send_message` → real Gmail `messages().send`, scoped to `gmail.send`;
  structured error mapping (auth → `AUTH_REQUIRED`, 403/403 → `AUTH_FAILED`,
  429 → `RATE_LIMITED`, 400 invalid sender/recipient, generic → `SEND_FAILED`).
- `refresh_credentials` → `credentials.refresh()` with a refreshed expiry.
- Provider error code vocabulary `ProviderErrorCode` (StrEnum) + `ProviderError`
  with stable codes for API mapping and audit.

## 3. OAuth flow

`authorization_url` builds a google-auth `Flow` with `offline` access, `consent`
prompt, no scope escalation (`include_granted_scopes=false`), and the System B
redirect URI. `complete(code, state)`:

1. Redeems the 600s single-use state (bound to tenant + user) via `GETDEL`.
2. Exchanges the code; rejects if granted scopes ⊄ requested scopes.
3. Extracts identity from the ID token (`sub`, `email`, `email_verified`);
   falls back to `users().getProfile` if claims are missing.
4. Persists tokens encrypted (new or rotated credential reference).
5. Creates (first connect) or re-connects the connection, audits
   `GOOGLE_CONNECTION_CREATED`/`GOOGLE_CONNECTION_VALIDATED`, commits atomically,
   and rolls back on `IntegrityError` (races → reauth).

## 4. Security posture

- State store fails closed without Redis in non-test environments; hashed state
  keys; atomic single-use redemption.
- Minimum scopes only; escalation guard; tokens never leave the backend.
- Callback is rate-limited and validates the connection belongs to the state's
  tenant; account-mismatch on reconnect is rejected.
- Frontend never sees secrets — only `credential_configured`/expiry/status.

## 5. Migration

`20260906_01_google_sender_connection_unique` adds a partial unique index
`uq_sender_connections_google_account` on
`(tenant_id, provider, external_account_id)` for `provider='GOOGLE'` rows with a
non-null external account id (SQLite + PostgreSQL predicates). Single Alembic
head verified. Service-level duplicate check returns 409 before the DB can
reject.

## 6. API endpoints

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| `POST` | `/api/v1/senders/google/authorize` | `integrations.connect` | `{ authorization_url }` |
| `GET` | `/api/v1/senders/google/oauth/callback` | public | browser redirect out/in |
| `POST` | `/api/v1/senders/{id}/test` | `integrations.connect` | `SenderTestResponse` |

## 7. UI changes

- `/integrations/google` page: Google connection card (status/connected/setup),
  Connect/Reconnect/Re-check, success/error banner from callback.
- Integrations catalog: GOOGLE Connect now routes to the OAuth page; GOOGLE
  `REAUTH_REQUIRED` connections show a Reconnect button.
- Sender accounts: Test button per active sender (recipient prompt, success
  shows message id).

## 8. Tests

- New: `tests/test_google_oauth_security.py` — state single-use, expiry, no
  echo, fail-closed without Redis.
- Updated: provider foundation (real send/discovery/identity mismatch),
  integrations (OAuth connect → `GOOGLE_CONNECTION_VALIDATED`, reauth mapping,
  duplicate 409, authorize 403/404, test-send success/422/401), spec alias
  endpoints (validate/discover/disconnect with real OAuth tokens).
- Backend full suite **525 passed**, 3 warnings.

## 9. Comparison to 10A

Nothing in 10A (`/src/`-free: providers/gmail, senders service, legacy senders
API, or `api/senders.py`) was touched. System B adds a *parallel* routing path
(`/senders/...`), scoped strictly to sending, with Clerk as identity and the
provider foundation as transport.

## 10. Remaining production blockers (unchanged + new)

1. Provision the real Google OAuth client, register
   `GOOGLE_SENDER_REDIRECT_URI` in the console, and pre-approve scopes for
   Workspace (internal consent screen).
2. **Redis is now required** for the OAuth flow — the state store fails closed
   without it (prod/ci must provide `REDIS_*`).
3. Clerk production instance/audience config (from 10A).
4. `20260905_29` + `20260906_01` must run on prod/staging before rollout.

## 11. Config

`GOOGLE_SENDER_REDIRECT_URI` (default
`http://localhost:8000/api/v1/senders/google/oauth/callback`), `GOOGLE_CLIENT_ID`,
`GOOGLE_CLIENT_SECRET` (existing), `REDIS_*` (existing).

## 12. Frontend UX

Described in §7. Gates: `tsc -b`, `eslint`, `vitest` (22), `vite build` green.

## 13. Verification checklist

- [x] OAuth authorize produces a consent URL (tested 403/404/configured paths)
- [x] Callback completes a code exchange and binds identity (integration tests)
- [x] Scope escalation rejected
- [x] State single-use, TTL, no-echo, fail-closed (security tests)
- [x] Tokens encrypted, rotated on reconnect
- [x] Duplicate account → 409 hint
- [x] Test send success + recipient validation + auth failures mapped
- [x] Migration single-head, index present
- [x] Frontend flows wired and gated

## 14. Test matrix

| Gate | Result |
| --- | --- |
| `pytest` (backend) | 525 passed |
| `ruff check app tests` | clean |
| mypy (new/changed) | clean (23 files) |
| `alembic heads` | single head `20260906_01` |
| `tsc -b` | clean |
| `eslint` | clean |
| `vitest run` | 22 passed |
| `vite build` | succeeded |

## 15. Summary

System B now has a production-grade Google sending path: a real Gmail adapter,
a secure consent flow with single-use encrypted state and scope enforcement,
identity-bound reconnect, DB-level duplicate protection, and a verifiable test
send — surfaced through a dedicated frontend page and inline sender actions.
Remaining go-live work is operational (Google client/console setup, Redis,
Clerk production config) rather than code.