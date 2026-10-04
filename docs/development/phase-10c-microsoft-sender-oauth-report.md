# Phase 10C — Microsoft 365 / Exchange Online Sender OAuth + Test Send (Report)

**Completed:** 2026-09-08
**Status:** Backend + frontend implemented; all gates green.

The Microsoft 365 integration (System B) is production-capable for sending: a
real Microsoft Graph adapter (send-only), end-to-end OAuth authorization-code
flow against Entra, encrypted token storage, identity-bound re-auth, duplicate
protection, and a live test-send round trip — all mirroring the Phase 10B Google
pattern.

---

## 1. Files changed

Backend (new):
- `backend/app/email_providers/microsoft/provider.py` — real Graph adapter
  (validate, `/me` discovery, `/me/sendMail`, refresh, error normalization)
- `backend/app/services/microsoft_sender_oauth.py` — OAuth flow service
- `backend/app/api/microsoft_sender_oauth.py` — authorize/callback/details/
  validate/disconnect/reconnect/refresh-credentials/test-send router
- `backend/app/alembic/versions/20260908_01_microsoft_sender_connection_unique.py`
- `backend/tests/test_microsoft_sender_oauth.py`
- `backend/tests/conftest.py` — test-only OAuth client env vars

Backend (modified):
- `backend/app/core/config.py` — `MICROSOFT_AUTHORITY`,
  `MICROSOFT_SENDER_REDIRECT_URI`
- `backend/app/schemas/integrations.py` — `MicrosoftAuthorize*`,
  `MicrosoftConnectionDetailsResponse`, `MicrosoftTestSendRequest`
- `backend/app/services/integrations.py` — MICROSOFT audit events, provider
  action helpers, `get_connection_details`, `get_sender_account_by_connection`,
  last-error tracking on validate
- `backend/app/models/entities.py` — partial unique index
- `backend/app/main.py` — mount `/senders/microsoft` router
- `backend/tests/test_email_provider_foundation.py`,
  `backend/tests/test_integrations.py` — MICROSOFT send-only capabilities

Frontend (new):
- `frontend/src/pages/MicrosoftIntegration.tsx`

Frontend (modified):
- `frontend/src/App.tsx` — `/integrations/microsoft` route
- `frontend/src/pages/Integrations.tsx` — MICROSOFT card → OAuth page, Reconnect
  action
- `frontend/src/api/integrations.ts` — `microsoftOAuthApi.*`
- `frontend/src/types/integrations.ts` — `Microsoft*` types

Docs / env:
- `docs/integrations/microsoft.md`
- `.env.example`, `.env.production.example`, `.env.staging.example` —
  `MICROSOFT_AUTHORITY`, `MICROSOFT_SENDER_REDIRECT_URI`

## 2. Real Microsoft Graph adapter

`app/email_providers/microsoft/provider.py`:
- `validate_connection` → `GET /me` (with
  `$select=id,mail,userPrincipalName,displayName`), identity re-check against the
  bound `microsoft_id`/email.
- `discover_senders` → the authenticated mailbox only (`/me`); the mailbox
  belongs to whatever account authorized the connection.
- `send_message` → `POST /me/sendMail` with `saveToSentItems`, structured error
  mapping (401 → `AUTH_FAILED`, 403 → `PERMISSION_DENIED`, 429 → `RATE_LIMITED`
  with `Retry-After`, 400 invalid recipient, 5xx → `PROVIDER_UNAVAILABLE`).
- Graph returns HTTP 202 for accepted sends; the provider surfaces a stable
  `accepted:{sha256(from|to|subject)[:16]}` message id.
- `refresh_credentials` → v2.0 token refresh with a refreshed expiry.
- `get_capabilities` → `supports_webhooks=false`, `supports_inbox_sync=false`
  (send-only; no mailbox-read scopes).

## 3. OAuth flow

`authorization_url` builds the Entra v2.0 consent URL
(`{MICROSOFT_AUTHORITY}/{MICROSOFT_TENANT_ID}/oauth2/v2.0/authorize`) with the
minimum delegated scope set (`openid profile email offline_access User.Read
Mail.Send`). `complete(code, state)`:

1. Redeems the 600s single-use state (bound to tenant + user) via the shared
   `OAuthStateStore` (atomic single consumption).
2. Exchanges the code at the v2.0 token endpoint; rejects if a refresh token is
   missing or granted scopes are not a subset of the requested set (escalation
   guard).
3. Resolves identity from `GET /me` (`id` → `external_account_id`, `mail` →
   `email`, fallback `userPrincipalName`).
4. Persists tokens encrypted (new or rotated credential reference via the
   existing Fernet pipes).
5. Creates (first connect) or re-connects the connection, audits
   `MICROSOFT_OAUTH_CONNECTED` / `MICROSOFT_CONNECTION_VALIDATED`, commits
   atomically, and rolls back on `IntegrityError` (races → reauth).

## 4. Security posture

- State store reused from Phase 10B: fails closed without Redis in non-test
  environments; hashed state keys; atomic single-use redemption; tenant/user
  binding.
- Minimum scopes only; escalation guard; tokens never leave the backend.
- Callback is public (browser navigation) but is protected by the state store's
  TTL + single use and per-IP rate limiting for `/senders/microsoft/`.
- Connection looked up inside the state's tenant; account-mismatch on reconnect
  is rejected; cross-tenant authorize/validate/disconnect/test-send → 404.
- Frontend never sees secrets — only `credential_configured`/expiry/status.
  `GET /senders/microsoft/connections/{id}` omits all credential fields.

## 5. Migration

`20260908_01_microsoft_sender_connection_unique` (down_revision `20260906_01`)
adds a partial unique index `uq_sender_connections_microsoft_account` on
`(tenant_id, provider, external_account_id)` for `provider='MICROSOFT'` rows with
a non-null external account id (SQLite + PostgreSQL predicates). Service-level
duplicate handling returns a "reconnect to refresh it" hint before the DB can
reject.

## 6. API endpoints

| Method | Path | Auth | Notes |
| --- | --- | --- | --- |
| `POST` | `/api/v1/senders/microsoft/authorize` | `integrations.connect` | `{ authorization_url }` |
| `GET` | `/api/v1/senders/microsoft/oauth/callback` | public | browser redirect out/in |
| `GET` | `/api/v1/senders/microsoft/connections/{id}` | `integrations.read` | safe details, no secrets |
| `POST` | `/api/v1/senders/microsoft/connections/{id}/validate` | `integrations.connect` | → CONNECTED/REAUTH/FAILED |
| `POST` | `/api/v1/senders/microsoft/connections/{id}/disconnect` | `integrations.disconnect` | → DISCONNECTED |
| `POST` | `/api/v1/senders/microsoft/connections/{id}/reconnect` | `integrations.connect` | `{ authorization_url }` |
| `POST` | `/api/v1/senders/microsoft/connections/{id}/refresh-credentials` | `integrations.connect` | rotate tokens |
| `POST` | `/api/v1/senders/microsoft/connections/{id}/test-send` | `integrations.connect` | explicit recipient |

## 7. UI changes

- `/integrations/microsoft` page: Microsoft connection card
  (status/connected/setup), Connect/Reconnect/Re-check, success/error banner from
  the callback.
- Integrations catalog: MICROSOFT Connect now routes to the OAuth page; MICROSOFT
  `REAUTH_REQUIRED` connections show a Reconnect button.
- Sender accounts: Microsoft senders appear alongside Google senders on
  `/senders` and support the standard recipient-prompt test flow.

## 8. Tests

- New: `tests/test_microsoft_sender_oauth.py` (21 tests) — provider unit tests
  (capabilities, connect, discover, send `accepted:` handle, invalid recipient,
  refresh rotation, 429/Retry-After + 401/403/500 normalization); OAuth security
  (authorize 403/404, cross-tenant 404, consent URL shape, bad-state, full
  callback round trip, state single-use/replay, scope escalation, token-exchange
  failure); endpoint lifecycle (details no-secrets, transition, disconnect,
  reconnect URL, test-send 400/404, unique index present).
- Updated: provider foundation + integrations capabilities tests now assert
  MICROSOFT is send-only (`supports_webhooks=false`, `supports_inbox_sync=false`).
- Backend full suite **546 passed**, 3 warnings.

## 9. Comparison to 10B

Microsoft reuses the Phase 10B architecture end to end (state store,
`SenderConnection` model, encrypted credential pipes, audit vocabulary, frontend
pattern). Differences are provider-specific: Entra v2.0 endpoints, delegated
`Mail.Send` scope set, send-only capabilities, `/me` identity binding, 202 =
accepted (not delivered), and 429 `Retry-After` normalization. Nothing in the
legacy System A Microsoft provider or `/api/v1/senders/microsoft/connect` was
touched; Phase 10C uses the distinct `/senders/microsoft/oauth/callback` path.

## 10. Remaining production blockers (unchanged + new)

1. Provision the real Entra OAuth client, register
   `MICROSOFT_SENDER_REDIRECT_URI` in the admin center, grant delegated
   `User.Read` + `Mail.Send` + `offline_access`, and obtain tenant admin consent
   if required.
2. **Redis is required** for the OAuth flow — the state store fails closed
   without it (prod/ci must provide `REDIS_*`).
3. `20260908_01` must run on prod/staging after `20260906_01`.

## 11. Config

`MICROSOFT_CLIENT_ID`, `MICROSOFT_CLIENT_SECRET` (existing),
`MICROSOFT_TENANT_ID` (default `common`), `MICROSOFT_AUTHORITY` (default
`https://login.microsoftonline.com`), `MICROSOFT_SENDER_REDIRECT_URI` (default
`http://localhost:8000/api/v1/senders/microsoft/oauth/callback`), `REDIS_*`
(existing).

## 12. Frontend UX

Described in §7. Gates: `tsc -b`, `eslint`, `vitest` (22), `vite build` green.

## 13. Verification checklist

- [x] OAuth authorize produces an Entra consent URL (403/404/configured paths)
- [x] Callback completes a code exchange and binds `/me` identity (integration tests)
- [x] Scope escalation rejected
- [x] State single-use, TTL, no-echo, fail-closed (reused store, security tests)
- [x] Tokens encrypted, rotated on reconnect
- [x] Duplicate account → reconnect hint
- [x] Test send success + recipient validation + auth failures mapped (401/429/400/502)
- [x] Send-only capabilities advertised (no webhooks/inbox sync)
- [x] Migration single-head (chains `20260906_01` → `20260908_01`), index present
- [x] Frontend flows wired and gated

## 14. Test matrix

| Gate | Result |
| --- | --- |
| `pytest` (backend) | 546 passed |
| `ruff check app tests` | clean |
| mypy (new/changed) | clean (5 files) |
| `tsc -b` | clean |
| `eslint` | clean |
| `vitest run` | 22 passed |
| `vite build` | succeeded |

## 15. Summary

System B now has production-grade sending paths for both Google (10B) and
Microsoft 365 (10C): a real Graph adapter, a secure Entra consent flow with
single-use encrypted state and scope enforcement, identity-bound reconnect,
DB-level duplicate protection, and a verifiable test send — surfaced through a
dedicated frontend page alongside the Google flow. Remaining go-live work is
operational (Entra client/console setup, Redis, Clerk production config) rather
than code.