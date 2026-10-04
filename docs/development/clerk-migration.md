# Clerk Authentication Migration (System A)

CR+CRM runs two identity systems. **System A** is a managed SaaS identity (Clerk)
that owns user accounts and sessions. **System B** is the app-owned tenant +
role/permission model (`users`, `tenants`, `roles`, `role_permissions`) that all
business data is scoped to.

This document explains how a Clerk session maps into System B, how the backend
verifies Clerk JWTs, and how the legacy email/password auth is being retired in a
controlled way.

## 1. Architecture at a glance

```
@clerk/react (frontend, /sign-in)        FastAPI (backend)
  └ getToken() ─── session JWT ───────►  app.identity.clerk.ClerkJWTVerifier
                                              │ RS256, JWKS cached
                                              ▼
                                         app.identity.dependencies.get_current_identity
                                              ▼
                                         app.security.permissions.get_current_principal
                                              │
                       ┌────────────────────────┴─────────────────────────┐
                       ▼ collections only when legacy_auth_enabled=true    │
              mapped / provisioned                           legacy HS256
              app user + roles                               principal
                       └───────────────────────────────────────────────────┘
                                              ▼
                                      tenant-scoped APIs
```

- Clerk owns sign-in/sign-up/session. Tenant-level authorization stays app-owned
  and is re-verified on every request (never trusted from the JWT).
- Legacy email/password endpoints remain available while `AUTH_LEGACY_ENABLED=true`
  (default). They are locked behind the same principal pipeline so one gate
  controls both worlds.

## 2. Backend components

| File | Responsibility |
|------|----------------|
| `app/identity/clerk.py` | `ClerkJWTVerifier`: RS256 verification against the Clerk JWKS URL, key `kid` caching + refresh, claim extraction. Raises `ClerkTokenError` / `ClerkNotConfiguredError`. |
| `app/identity/dependencies.py` | `get_current_identity` — strict Clerk-only dependency (used by tests / future Clerk-only routes). |
| `app/identity/mapping.py` | `IdentityMapper` — resolves a verified Clerk identity to an app user; links or provisions. |
| `app/identity/schemas.py` | `VerifiedIdentity` dataclass + placeholder-email helper. |
| `app/security/permissions.py` | `get_current_principal`: tries Clerk first, then (gated) legacy token decode. `require_permission` bypass set is `{ADMIN, SUPER_ADMIN, OWNER}`. |
| `app/api/auth.py` | Legacy routes gated by `_require_legacy_auth()`; `/auth/me` still serves both identities. |

## 3. User provisioning & linking

For each request the backend:

1. Verifies the Clerk JWT (issuer + audience must match `CLERK_ISSUER` /
   `CLERK_AUDIENCE`; signature checked against the cached JWKS).
2. Looks up `users.external_identity_id = <clerk sub>`.
3. If found → loads the user (idempotent; `IntegrityError` is repaired by
   re-fetching on the external id).
4. If not found and the token carries an email with exactly one user match →
   **links** the existing account to the Clerk identity (legacy-to-Clerk
   migration path; audit event `CLERK_IDENTITY_LINKED`).
5. Otherwise → **provisions** a new tenant, assigns the `OWNER` system role,
   grants the `WORKSPACE_PERMISSION_KEYS` bundle, and stores an SECRET-mail
   placeholder (`clerk-<sub>@identity.local`) when the token has no email.
   `password_hash` stays `NULL`.

Provisioning always commits; a concurrent duplicate is rolled back to a savepoint
and re-fetched. The provisioned role is `OWNER` (full workspace rights, never
`ADMIN`).

## 4. Migration schema

Migration `20260905_29_clerk_identity` adds:

- `users.external_identity_id` varchar(100), unique (`ix_users_external_identity_id`)
- `users.identity_provider` varchar(20), server default `'CLERK'`

Current head: `20260905_29` (down_revision `20260903_28`).

## 5. Configuration

| Env | Default | Meaning |
|-----|---------|---------|
| `CLERK_ISSUER` | empty (Clerk disabled) | Clerk instance issuer; leave empty to disable Clerk verification entirely |
| `CLERK_JWKS_URL` | empty | JWKS endpoint for key rotation |
| `CLERK_AUDIENCE` | empty | JWT audience required on each token (blank accepts any) |
| `AUTH_LEGACY_ENABLED` | `true` | when `false`, legacy signup/login/refresh/password-reset are rejected |

Frontend: `VITE_CLERK_PUBLISHABLE_KEY` + `@clerk/react` (`main.tsx`,
`SignIn.tsx`/`SignUp.tsx`, `App.tsx` session driver). Instance used in this phase:
`vocal-escargot-1567.clerk.accounts.dev`.

## 6. Controlled legacy retirement

- Nothing legacy is deleted — it is gated, so running tenants are unaffected
  until the operator flips the flag.
- Both paths share `get_current_principal`, so every protected route benefits
  from the same ownership/role checks regardless of identity source.
- Flip `AUTH_LEGACY_ENABLED=false` only after all tenants have linked their Clerk
  identity and legacy tokens have been retired.

## 7. Security notes

- Secrets in system-B integration connections are encrypted at rest; responses
  expose only a `credential_configured` flag.
- The identity pipeline logs audit events through the existing audit service with
  secrets stripped.
- `ClerkJWTVerifier` never caches decoded tokens; JWKS cache refresh is driven by
  unknown `kid`.