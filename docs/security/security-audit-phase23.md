# Phase 23 — Final Security Audit

**Date:** 2026-09-01
**Scope:** Full backend + frontend security review across AUTH, RBAC, MULTI-TENANCY, API, EMAIL, AI, SECRETS, and DEPENDENCIES. Critical and High findings were remediated; Medium/Low items documented with residual risk.

## Summary

The system was already security-hardened from prior phases (scrypt password hashing, JWT HS256 with refresh rotation and family reuse detection, Redis rate-limiting middleware, Fernet-encrypted provider credentials, JWT-signed single-use OAuth state, tenant-scoped repositories, HMAC webhook verification with replay-window and idempotency, AI evidence-bounded drafts that never auto-send, and CSS sanitization via `nh3`/DOMPurify).

This audit verified those controls, fixed 1 HIGH (AI-draft approval bypass) and 1 MED/HIGH (cross-tenant ops exposure), remediated both Python and frontend dependency vulnerabilities, removed a committed private key, and documented residual risks.

| # | Area | Finding | Severity | Status |
|---|------|---------|----------|--------|
| 1 | AI / API | `InboxReplyService.send()` allowed sending an AI reply draft that was never approved | HIGH | **Fixed** |
| 2 | MULTI-TENANCY / RBAC | `/ops/*` endpoints exposed cross-tenant platform data to any tenant admin | MED/HIGH | **Fixed** |
| 3 | DEPENDENCIES | Python `cryptography 46.0.7` — 4 advisories (PYSEC-2026-3552/3553/3554, GHSA-537c-gmf6-5ccf) | CRITICAL | **Fixed** |
| 4 | DEPENDENCIES | Frontend `vitest 2.1.9` (CRITICAL dev), `vite <=6.4.2` (HIGH dev), `react-router-dom 6.30.6` (MODERATE prod open-redirect CVE-2025-68470) | CRITICAL/HIGH | **Fixed** |
| 5 | SECRETS | `nginx/certs/tls.key` committed to git | MED (policy) | **Fixed** |
| 6 | MULTI-TENANCY / API | Delivery-event webhook uses a single shared provider secret across all tenants | MED | Documented (residual) |
| 7 | EMAIL | Send/approval compliance gates, webhook replay+dedup verified safe | — | Verified |
| 8 | AUTH | Password hashing, JWT, refresh rotation, rate limiting verified safe | — | Verified |

---

## 1. Audit by category

### 1.1 AUTH — verified SAFE
- Passwords: scrypt hashing with 12-char minimum (`security/passwords.py`).
- Tokens: JWT HS256 access tokens; refresh tokens with rotation, family/reuse detection, and invalidation on rotate/logout (`security/tokens.py`, `services/auth.py`).
- Rate limiting: Redis Lua script in `main.py` middleware (login 5/min and per-route limits), fail-tolerant.
- Password reset / change flows enforce old-password confirmation; generic login error messages prevent user enumeration.

### 1.2 RBAC / MULTI-TENANCY
- `require_permission` (`security/permissions.py`) resolves roles scoped to the caller's tenant; ADMIN/SUPER_ADMIN still pass the tenant check because roles are tenant-scoped.
- Tenant-scoped repository pattern applied consistently; baseline tests `test_security_foundation.py`, `test_tenant_access.py`, `test_admin_security.py` all pass.
- **Finding #2 (FIXED):** `/ops/*` (observability, alerts, metrics) was gated only by the tenant-scoped `settings.manage`, but `OpsService`/`AlertService` query **global** cross-tenant rows (delivery jobs, alert records, `OpsMetricSample` carrying `tenant_id`). A tenant admin could therefore read platform-wide ops data.
  - **Fix:** added a `require_super_admin` dependency (`security/permissions.py`) that requires a `SUPER_ADMIN` role, and applied it to all `/ops/*` routes (`api/ops.py`). Only platform super-admins can now read cross-tenant ops data.
  - **Regression test:** `test_ops_api.py::test_ops_endpoints_require_super_admin` asserts a tenant admin gets `403` and a super-admin gets `200`.

### 1.3 API
- Input validation via Pydantic models with length/type bounds throughout.
- Unauthenticated access returns `401`; insufficient permission returns `403`.
- Webhook endpoints reject unverified events before any state change.

### 1.4 EMAIL
- Verified safe: suppression/unsubscribe/bounce/complaint engine (`services/suppression.py`, `services/compliance.py`), sender-health throttling, HMAC-signed tokenized unsubscribe (48-byte urlsafe token), and webhook verification with 300s replay window + idempotency (`services/webhooks.py`).
- **Finding #1 (FIXED):** `api/inbox.py::send_reply` allowed a caller to reference an AI reply draft via `draft_id` in `InboxReplyService.send()` without requiring the draft to be `APPROVED`; the draft was marked `SENT` unconditionally. A user could thus send AI-generated (potentially prompt-injection-flagged) content while skipping the human review/approval gate.
  - **Fix:** `InboxReplyService.send()` now calls `_require_approved_draft(draft_id, thread_id)` which blocks the send unless the draft exists in the tenant, belongs to the same thread, and has `status == "APPROVED"`. Manual sends without a `draft_id` (user-composed content) are unaffected.
  - **Regression test:** `test_ai_assistant.py::test_send_requires_approved_draft` verifies an unapproved draft and a wrong-thread draft are both blocked.

### 1.5 AI
- Verified: AI message drafts (`AIMessageDraft`) require approval before use as a campaign template; AI reply drafts require approval before send (now enforced, see #1).
- Prompt-injection detection sets `REVIEW_REQUIRED` (`services/ai_safety.py`, `ai_drafts.py`, `ai_assistant.py`); AI never auto-sends (evidence-bounded drafts / manual send only).
- AI provider is adapter-based (`providers/factory.py`); only the `mock` provider is wired in this environment. **Note (residual risk):** the mock provider does not run prompt-injection detection on the inbound message content that it consumes to draft a reply; this is acceptable for the current mock-only configuration but a real provider should apply detection on inbound content.

### 1.6 SECRETS
- No AWS/GitHub/Slack/OpenAI high-signal secrets in tracked files.
- **Finding #5 (FIXED):** `nginx/certs/tls.key` (a self-signed `localhost` dev key, not a production secret) was git-tracked. Private key material must never be committed.
  - **Fix:** `git rm --cached nginx/certs/tls.key nginx/certs/tls.crt` (files retained on disk for local nginx); `.gitignore` now excludes `nginx/certs/*.key|*.crt|*.pem`, `**/*.pem`, `**/*.p8`, `**/*.pfx`, `**/*.jks`.
  - Added `infrastructure/nginx/gen_self_signed_certs.ps1` to regenerate the dev-only self-signed cert reproducibly.

### 1.7 DEPENDENCIES
- **Python (finding #3, FIXED):** `pip-audit` reported 4 advisories in `cryptography 46.0.7`. `requirements.txt` now pins `cryptography>=50.0.0,<51`; verified Fernet round-trip still works and `pip-audit -r requirements.txt -l` returns **"No known vulnerabilities found"**.
- **Frontend (finding #4, FIXED):** `npm audit` reported:
  - CRITICAL `vitest 2.1.9` (GHSA-5xrq-8626-4rwp) and HIGH nested `vite >=6.0.0 <6.4.3` / `vite-node` / `esbuild` — all dev-only (exploitable only when the UI test server is exposed).
  - MODERATE production `react-router-dom 6.30.6` open-redirect (CVE-2025-68470).
  - Upgraded: `react-router-dom@7.18.3`, `vite@6.4.3`, `vitest@4.1.11` (all sharing vite 6.4.3, no nested vulnerable copy).
  - Result: `npm audit` reports **0 vulnerabilities**. `npm test` = 20 passing, `npm run build` (tsc + vite) succeeds, `npm run lint` clean.
  - **Test corrections (pre-existing, unrelated to the upgrade):** `OpsCenter.test.tsx` used ambiguous/exact text queries that were already failing; replaced with `getAllByText`/regex assertions so the suite is green.

---

## 2. Residual risk (accepted / documented)

| Risk | Severity | Rationale / Mitigation |
|------|----------|------------------------|
| **Shared webhook secret across tenants** (`settings.webhook_secrets` is a single configured secret; `tenant_id` is in the `/delivery-events/{provider}/{tenant_id}` path and attacker-supplied) | MED | Anyone who obtains the shared HMAC secret could post events for any tenant. The secret is server-side, env-supplied, and never exposed. Proper fix is per-provider/per-tenant secrets — a larger change deferred. Mitigate by rotating `WEBHOOK_SECRETS` and keeping it out of client bundles. |
| **`/health/details` gated by `settings.manage`** | LOW | Returns readiness/config, not tenant data; acceptable, but consider super-admin gating to match `/ops/*`. |
| **AI mock provider skips inbound prompt-injection detection for replies** | LOW | Mock-only environment; real provider should scan inbound content (see 1.5). |
| **Unsubscribe token not invalidated after single use / no expiry** | LOW | Tokens are 48-byte urlsafe (high entropy); acceptable brute-force resistance. |
| **Rate-limit fail-open in sending** | LOW | Redis unavailability degrades (not blocks) sending; documented trade-off to avoid outage. |

---

## 3. Commands run to verify

```bash
# Baseline security tests (pass): 11 passed
.venv/Scripts/python.exe -m pytest tests/test_security_foundation.py \
    tests/test_tenant_access.py tests/test_admin_security.py tests/test_webhooks.py -q

# Full backend suite after fixes: 450 passed
.venv/Scripts/python.exe -m pytest -q

# New regression tests
.venv/Scripts/python.exe -m pytest tests/test_ops_api.py tests/test_inbox.py tests/test_ai_assistant.py -q

# Lint
.venv/Scripts/python.exe -m ruff check app/api/ops.py app/security/permissions.py app/services/inbox_reply.py

# Python deps
.venv/Scripts/python.exe -m pip-audit -r requirements.txt -l   # "No known vulnerabilities found"

# Frontend
npm audit                     # 0 vulnerabilities
npm test                      # 20 passed
npm run build                 # tsc + vite build OK
npm run lint                  # clean
```

---

## 4. Files changed in this phase

**Backend (security fixes):**
- `backend/app/security/permissions.py` — added `require_super_admin`.
- `backend/app/api/ops.py` — `/ops/*` now gated by `require_super_admin`.
- `backend/app/services/inbox_reply.py` — enforce AI-draft approval before send.
- `backend/tests/test_ops_api.py` — updated for super-admin-only access + regression.
- `backend/tests/test_ai_assistant.py` — regression: send requires approved draft.
- `backend/requirements.txt` — `cryptography>=50.0.0,<51`.

**Frontend (dependency remediation):**
- `frontend/package.json`, `frontend/package-lock.json` — `react-router-dom@7.18.3`, `vite@6.4.3`, `vitest@4.1.11`.
- `frontend/src/pages/OpsCenter.test.tsx` — fixed pre-existing ambiguous text queries.

**Secrets / infra:**
- `.gitignore` — exclude private key / cert material.
- `nginx/certs/tls.key`, `nginx/certs/tls.crt` — removed from git tracking (`git rm --cached`).
- `infrastructure/nginx/gen_self_signed_certs.ps1` — new dev cert generator.

**Docs:**
- `docs/security/security-audit-phase23.md` — this document.
