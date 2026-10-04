# Email Provider Foundation (System B)

Phase 10A adds an architecture-only email-provider foundation alongside the
existing live sender stack (`app/providers/`, `app/services/senders.py`).
It deliberately implements **no real provider calls**; it defines the seam,
the credential lifecycle, and the API surface that the next phase will fill in.

## Design intent

- Existing Gmail/Microsoft/SMTP sender code is untouched and remains the live
  path used by campaigns.
- The foundation lives in `app/email_providers/` with the ABC
  `EmailProviderBase` and one provider module per provider
  (`google`, `microsoft`, `zoho`, `sendgrid`, `smtp`).
- Provider logic that requires network access raises
  `ProviderMethodNotImplemented`. Local, deterministic behavior is stubbed:
  for example `discover_senders` only returns senders when a connection is
  explicitly seeded with `simulated_discovery` metadata, so tests and demos do
  not depend on live provider APIs.
- Capabilities are advertised via `ProviderCapabilities`
  (`supports_oauth`, `supports_api_key`, `supports_smtp`, `supports_sender_discovery`,
  `supports_webhooks`, `supports_inbox_sync`).

## Data model

- `sender_connections`: one row per provider connection/account, tenant-scoped.
  Credentials are never stored in plaintext; `credential_reference` holds an
  encrypted blob reference and `credential_version` tracks rotation (v1, v2, ...).
  `metadata` (mapped attribute `connection_metadata` because `metadata` is
  reserved in SQLAlchemy) carries connection-level configuration such as SMTP
  host/port. The `email` column is the SMTP envelope address.
- `sender_accounts`: concrete senders discovered/registered for a connection
  (`ACTIVE`, `DISABLED`, `PENDING_VERIFICATION`, `INVALID`).
- Migration: `app/alembic/versions/20260903_28_email_providers_foundation.py`.

## Credential lifecycle

All secrets travel through `app/email_providers/credentials.py`:

- `encrypt_credential_reference` -> encrypted Fernet blob (using the existing
  `CredentialStore` / `ENCRYPTION_KEY`).
- `rotate_credential_reference` -> new reference at the next version (v1 -> v2).
- `invalidate_credential_reference` -> tombstone (`revoked:vN`); tombstoned
  references fail decryption on purpose.
- Only a fixed allow-list of fields may be stored
  (`api_key`, `smtp_password`, `client_secret`, `access_token`, `refresh_token`,
  `client_id`, `smtp_username`, `tenant_id`, `scopes`, `expires_at`).

Credentials are accepted once over the API and never returned. Responses expose
only `credential_configured`. Secrets are never written to audit logs.

## API surface

Registered at `/api/v1/integrations`:

- `GET /providers` — capability catalogue.
- `POST /` — create a connection (validates provider + connection type).
- `GET / | GET /{id}` — list/get connections (tenant-scoped).
- `POST /{id}/credentials` — one-time credential upload.
- `POST /{id}/validate` — validate connection against stored credentials.
- `POST /{id}/refresh-credentials` — rotation for re-auth flows.
- `POST /{id}/discover` — sender discovery (simulated in this phase).
- `GET|POST /{id}/senders`, `PATCH /senders/{sender_id}`, `DELETE /{id}` —
  sender account lifecycle.
- `POST /{id}/disconnect` — disconnect/disable a connection.

Permissions: `integrations.read`, `integrations.connect`,
`integrations.disconnect`, `integrations.discover` (seeded via
`DEV_PERMISSIONS`, `initialize_database`, and signup bootstrap).
All mutations emit audit events under the `sender_connection` / `sender_account`
topics.

## Testing

- `tests/test_integrations.py` — full API/RBAC/cross-tenant/credential-exposure
  coverage using the repo's per-test sqlite fixture pattern.
- `tests/test_email_provider_foundation.py` — registry, capability, local shape
  rules, credential round-trip/rotation/revocation, and the explicitly inert
  send/discover boundaries.

## Phase boundary

No real provider traffic happens yet. `send_message`, `sync_inbox`,
`get_delivery_events`, and webhook handling raise
`ProviderMethodNotImplemented`. Enabling real providers is a follow-up phase and
must keep these seams: tenant scoping, never-plaintext credentials, audit events,
and no secrets over the wire beyond the one-time upload endpoint.