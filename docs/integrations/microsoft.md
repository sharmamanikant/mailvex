# Microsoft 365 / Exchange Online Sender OAuth Integration (System B)

**Phase:** 10C — Production Microsoft 365 sender connection
**Completed:** 2026-09-08
**Status:** Backend + frontend implemented; all gates green.

## Overview

System B's email provider foundation included a real Microsoft provider adapter
but no way to obtain production Microsoft credentials. This phase wires the
complete OAuth 2.0 authorization-code flow for Microsoft 365 / Exchange Online
sender connections: consent URL, server-side code exchange against Microsoft
Entra, token encryption, identity binding, re-auth recovery, and a live
test-send path through Microsoft Graph.

The integration mirrors the Phase 10B Google flow (System B
`SenderConnection`/`SenderAccount` abstraction) while using Microsoft-specific
behavior: delegated scopes, the Entra v2.0 consent endpoint, Graph `/me`
identity resolution, and Graph `POST /me/sendMail` acceptance (HTTP 202).

Clerk remains the application identity provider. Microsoft OAuth is a separate,
*authorization* context: it proves the tenant controls a Microsoft 365 account
and grants sending access to a mailbox.

## Scopes & least privilege

Only the minimum delegated scopes required to send are requested:

- `openid`
- `profile`
- `email`
- `offline_access`
- `User.Read`
- `Mail.Send`

No mailbox-read, inbox-sync, or Graph webhook scopes are requested, so the
provider advertises `supports_webhooks=false` and `supports_inbox_sync=false`.
The callback verifies that granted scopes (echoed by the token endpoint) are a
subset of the requested minimum set and rejects any escalation.

## Security design

- **State is single-use, expiring, and redemption-keyed.** The Phase 10A/10B
  `oauth_state_store.py` is reused unchanged: Redis keys `crcrm:oauth:state:{sha256(state)}`
  keyed by the state's SHA-256, TTL 600s, atomic single consumption, bound to the
  initiating tenant + user. In test environments the store falls back to an
  in-memory map; in production, without Redis, it **fails closed**.
- **Tenant/user never come from the callback URL.** They are resolved from the
  consumed state, and the connection is looked up inside that tenant only.
- **Tokens encrypted at rest** via the existing Fernet credential pipes
  (`encrypt_credential_reference` / `rotate_credential_reference`). Access,
  refresh, and client secrets never appear in responses, logs, audit records,
  redirect URLs, or the frontend.
- **Identity binding.** The Graph `/me` `id` becomes `external_account_id`; the
  Graph `mail` (falling back to `userPrincipalName`) is stored on the connection.
  Validation re-checks the `/me` profile against the bound identity, and
  reconnects reject an identity that doesn't match the connection.
- **Distinct callback path.** `/api/v1/senders/microsoft/oauth/callback`
  (configurable via `MICROSOFT_SENDER_REDIRECT_URI`), leaving the earlier System A
  callback route untouched.
- **Send-only acceptance.** Graph `POST /me/sendMail` returns HTTP 202 (accepted,
  not delivered). The provider returns a stable `accepted:{hash}` message id and
  sends are never reported as "delivered".

## Endpoints (all under `/api/v1`)

| Method | Path | Auth | Access |
| --- | --- | --- | --- |
| `POST` | `/senders/microsoft/authorize` | Clerk | `integrations.connect` |
| `GET` | `/senders/microsoft/oauth/callback?code=&state=` | public (server-side state) | browser redirect |
| `GET` | `/senders/microsoft/connections/{id}` | Clerk | `integrations.read` |
| `POST` | `/senders/microsoft/connections/{id}/validate` | Clerk | `integrations.connect` |
| `POST` | `/senders/microsoft/connections/{id}/disconnect` | Clerk | `integrations.disconnect` |
| `POST` | `/senders/microsoft/connections/{id}/reconnect` | Clerk | `integrations.connect` |
| `POST` | `/senders/microsoft/connections/{id}/refresh-credentials` | Clerk | `integrations.connect` |
| `POST` | `/senders/microsoft/connections/{id}/test-send` | Clerk | `integrations.connect` |

- `authorize` requires an existing `MICROSOFT` `SenderConnection` (404 otherwise)
  and returns `{ authorization_url }` targeting
  `{MICROSOFT_AUTHORITY}/{MICROSOFT_TENANT_ID}/oauth2/v2.0/authorize`.
- The callback exchanges `code` for tokens at the v2.0 token endpoint, resolves
  `/me`, binds identity, creates or re-connects the connection, and redirects to
  `/{frontend-origin}/integrations/microsoft?connected=1` (`?error=...` on
  failure).
- `validate` re-checks the connection against Graph. Permanent auth failures map
  to `REAUTH_REQUIRED`; verification failures to `FAILED`.
- `test-send` validates the recipient and sends via Graph `POST /me/sendMail`.
  Provider auth failures map to `401`, rate limits to `429` (with `Retry-After`),
  invalid recipient/message rejection to `400`, and generic send failures to `502`.

## Lifecycle behavior

- **First connect:** OAuth completes → connection transitions to `CONNECTED`
  (audit `MICROSOFT_OAUTH_CONNECTED`).
- **Reconnect:** the same bound account re-authorizes (connector's
  `REAUTH_REQUIRED` state) → tokens rotated, connection re-validated (audit
  `MICROSOFT_CONNECTION_VALIDATED`).
- **Validation:** permanent token/auth failures map to `REAUTH_REQUIRED` (audit
  `MICROSOFT_CONNECTION_REAUTH_REQUIRED`); verification failures to `FAILED`
  (audit `MICROSOFT_CONNECTION_FAILED`).
- **Duplicate protection:** a partial unique index
  (`uq_sender_connections_microsoft_account`) blocks two `MICROSOFT` connections
  with the same `external_account_id` within a tenant; the create/authorize path
  returns 409 with a "reconnect to refresh it" hint.
- **OAuth events:** `MICROSOFT_OAUTH_STARTED`, `MICROSOFT_OAUTH_FAILED`,
  `MICROSOFT_OAUTH_CONNECTED`, `MICROSOFT_CONNECTION_VALIDATED`.

## Migration

`20260908_01_microsoft_sender_connection_unique` (down_revision `20260906_01`):
partial unique index on `(tenant_id, provider, external_account_id)` where
`provider = 'MICROSOFT' AND external_account_id IS NOT NULL` (SQLite + PostgreSQL
predicate variants). The `SenderConnection.__table_args__` carries symmetrical
metadata.

## Frontend

- `/integrations/microsoft` (`MicrosoftIntegration.tsx`): connection status card,
  Connect/Reconnect/Re-check actions, status banner driven by the callback
  redirect params.
- `/integrations` (`Integrations.tsx`): the MICROSOFT provider card now routes to
  the OAuth page instead of the generic credential wizard; `MICROSOFT`
  connections in `REAUTH_REQUIRED` show a Reconnect action.
- `/senders` (`Senders.tsx`): Microsoft senders appear alongside Google senders;
  active senders support the Test flow (recipient prompt → test-send).

## Configuration

| Env var | Purpose | Default |
| --- | --- | --- |
| `MICROSOFT_CLIENT_ID` / `MICROSOFT_CLIENT_SECRET` | Entra OAuth client | — |
| `MICROSOFT_TENANT_ID` | Entra tenant (`common` for personal/any org) | `common` |
| `MICROSOFT_AUTHORITY` | Entra authority base | `https://login.microsoftonline.com` |
| `MICROSOFT_SENDER_REDIRECT_URI` | System B callback URI | `http://localhost:8000/api/v1/senders/microsoft/oauth/callback` |

In the Microsoft Entra admin center: register a Web application, add the exact
redirect URI, grant delegated permissions `User.Read`, `Mail.Send`, and
`offline_access` (plus `openid`/`profile`/`email`), and obtain tenant admin
consent if the org policy requires it.

## Operational notes

- Tokens are never returned to the frontend; `credential_configured` and expiry
  status are the only exposure.
- A failed or expired authorization leaves the connection in its previous state;
  auditing marks the attempt via `MICROSOFT_OAUTH_FAILED`.
- Graph acceptance (HTTP 202) is not a guarantee of delivery; treat the
  `accepted:` message id accordingly and configure sender/domain compliance
  independently.
- Test-send failures record the provider error code/reason in the audit event
  but never the message body or secrets.
- After go-live, confirm the production Redis instance is configured (the OAuth
  state store fails closed without it).

## Test results

- Backend: full `pytest` suite green (546 tests) including
  `tests/test_microsoft_sender_oauth.py`; `ruff check app tests` clean; mypy
  clean on all new/changed app code (pre-existing baseline errors in untouched
  files only).
- Frontend: `tsc -b` clean, `eslint` clean, `vitest run` green (22 tests),
  `vite build` succeeded.