# Google Sender OAuth Integration (System B)

**Phase:** 10B — Production Google Workspace/Gmail sender connection
**Completed:** 2026-09-07
**Status:** Backend + frontend implemented; all gates green.

## Overview

System B's email provider foundation included a real Google provider adapter but
no way to obtain production Google credentials. This phase wires the complete
OAuth 2.0 authorization-code flow for Google sender connections: consent URL,
server-side code exchange, token encryption, identity binding, re-auth recovery,
and a live test-send path.

Clerk remains the application identity provider. Google OAuth is a separate,
*authorization* context: it proves the tenant controls a Google account and
grants outlook-limited access to a Gmail mailbox for sending.

## Scopes & least privilege

Only the minimum scopes required to send are requested:

- `openid`
- `https://www.googleapis.com/auth/userinfo.email`
- `https://www.googleapis.com/auth/gmail.send`

No inbox-read, label, or sync scopes. The callback verifies that granted scopes
are a subset of requested scopes and rejects any escalation
(`gmail.send`-only capabilities cannot be widened from the consent screen).

## Security design

- **State is single-use, expiring, and redemption-keyed.** `oauth_state_store.py`
  stores `Redis` keys `crcrm:oauth:state:{sha256(state)}` keyed by the state's
  SHA-256, TTL 600s, and redeems with `GETDEL` (atomic single consumption). In
  test environments the store falls back to an in-memory map; in production,
  without Redis, it **fails closed** (raises, never downgrades silently).
- **Bound to the initiating tenant + user.** The state payload records both, and
  the callback resolves the connection inside that tenant only.
- **Tokens encrypted at rest** via the existing Fernet credential pipes
  (`encrypt_credential_reference` / `rotate_credential_reference`). Refresh
  tokens, access tokens, and client secrets never appear in audit logs,
  responses, redirect URLs, or the frontend.
- **Identity binding.** The Google `sub` claim becomes `external_account_id`; the
  Gmail `email` is stored on the connection. Validation re-checks the Gmail
  profile email against the bound identity.
- **Distinct callback path.** The new flow uses
  `/api/v1/senders/google/oauth/callback` (configurable via
  `GOOGLE_SENDER_REDIRECT_URI`), leaving the earlier System A callback route
  untouched.

## Endpoints (all under `/api/v1`)

| Method | Path | Auth | Access |
| --- | --- | --- | --- |
| `POST` | `/senders/google/authorize` | Clerk | `integrations.connect` |
| `GET` | `/senders/google/oauth/callback?code=&state=` | public (server-side state) | browser redirect |
| `POST` | `/senders/{id}/test` | Clerk | `integrations.connect` |

- `authorize` requires an existing `GOOGLE` `SenderConnection` (404 otherwise);
  returns `{ authorization_url }`. The frontend redirects the browser there.
- The callback exchanges `code` for tokens, binds identity, creates or
  re-connects the connection, and redirects back to
  `/{frontend-origin}/integrations/google?connected=1` (or `?error=...` on
  failure). Rate-limited at 10 req / 5 min.
- `test` validates the recipient, sends via the connected Gmail provider, and
  audits the result. Provider auth failures map to `401`; rate limits to `429`
  (with `Retry-After`); invalid recipient/message rejection to `400`; generic
  send failures to `502`.

## Lifecycle behavior

- **First connect:** OAuth completes → connection transitions to `CONNECTED`
  (audit `GOOGLE_CONNECTION_CREATED`).
- **Reconnect:** the same bound account re-authorizes (connector's
  `REAUTH_REQUIRED` state) → tokens rotated, connection re-validated (audit
  `GOOGLE_CONNECTION_VALIDATED`).
- **Validation:** permanent token/auth failures map to `REAUTH_REQUIRED`
  (audit `GOOGLE_CONNECTION_REAUTH_REQUIRED`); verification failures to `FAILED`
  (audit `GOOGLE_CONNECTION_FAILED`).
- **Duplicate protection:** a partial unique index
  (`uq_sender_connections_google_account`) blocks two `GOOGLE` connections with
  the same `external_account_id` within a tenant; the create path returns 409
  with a "reconnect to refresh it" hint.

## Migration

`20260906_01_google_sender_connection_unique` (down_revision `20260905_29`):
partial unique index on `(tenant_id, provider, external_account_id)` where
`provider = 'GOOGLE' AND external_account_id IS NOT NULL` (SQLite + PostgreSQL
predicate variants).

## Frontend

- `/integrations/google` (`GoogleIntegration.tsx`): connection status card,
  Connect/Reconnect/Re-check actions, status banner driven by the callback
  redirect params.
- `/integrations` (`Integrations.tsx`): the GOOGLE provider card now routes to
  the OAuth page instead of the generic credential wizard; `GOOGLE` connections
  in `REAUTH_REQUIRED` show a Reconnect action.
- `/senders` (`Senders.tsx`): each active sender row gains a Test button that
  prompts for a recipient and calls the test-send endpoint.

## Configuration

| Env var | Purpose | Default |
| --- | --- | --- |
| `GOOGLE_CLIENT_ID` / `GOOGLE_CLIENT_SECRET` | OAuth client (existing) | — |
| `GOOGLE_SENDER_REDIRECT_URI` | System B callback URI | `http://localhost:8000/api/v1/senders/google/oauth/callback` |

Register this redirect URI in the Google Cloud console as an authorized
redirect for the OAuth client. Workspace admins must also approve the
`gmail.send` / `userinfo.email` / `openid` scope combination if the OAuth
consent screen is configured for internal use.

## Operational notes

- Tokens are never returned to the frontend; `credential_configured` and expiry
  status are the only exposure.
- A failed or expired authorization leaves the connection in its previous state;
  auditing marks the attempt via `GOOGLE_CONNECTION_FAILED`.
- Test-send failures record the provider error code/reason in the audit event
  but never the message body or secrets.
- After go-live, confirm the production Redis instance is configured (the OAuth
  state store fails closed without it).

## Test results

- Backend: full `pytest` suite green (515 → 519+ with the new
  `tests/test_google_oauth_security.py` and integration additions); `ruff check
  app tests` clean; mypy clean on all new/changed app code (pre-existing
  baseline errors in untouched files only).
- Frontend: `tsc -b` clean, `eslint` clean, `vitest run` green, `vite build`
  succeeded.