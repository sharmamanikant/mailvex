# Phase 5 Report — Sender Health Engine

**Project:** CR+CRM (email-sender provisioning platform)
**Phase scope:** Delivery-health evaluation for workspace Senders — provider connection, mailbox state, domain DNS (SPF/DKIM/DMARC/MX), sending configuration, and sending signals — with documented, versioned, transparent scoring; paginated history; and a purpose-built frontend (detail health panel + directory health column).
**Status:** COMPLETE — backend 71 new tests green (58 engine + 13 API) plus the legacy System-B and warmup/quota suites re-verified; full non-Redis backend suite **746 passed / 1 deselected**; frontend **63 tests green**, lint + tsc/build clean; ruff + mypy (strict) clean for all Phase 5 files. Alembic head `20260916_04`.

---

## 1. Objective & Stop Condition

Phase 5 delivers an observed-signal health engine for the Sender entity created in Phases 3–4. It never fabricates a score, never guesses DNS records, never claims inbox-placement guarantees, and is explicitly **not** a warmup engine. The phase is complete only when each acceptance check passes:

1. Health engine evaluates a Sender across provider connection, mailbox state, DNS (SPF/DKIM/DMARC/MX), sending configuration, and sending signals.
2. Scoring is deterministic, **versioned** (`v1`), and transparent — every score ships with its weights, thresholds, and unknown-handling rule.
3. UNKNOWN / NOT_APPLICABLE checks are never counted as zero; their weight is redistributed proportionally, and a check with no known result yields an UNKNOWN overall verdict.
4. Any FAIL with severity CRITICAL forces CRITICAL; any other FAIL forces at most WARNING — a failed check always prevents HEALTHY.
5. No credentials, tokens, or secret payloads appear in any log, API response, or stored row.
6. DNS/Redis outages fail gracefully to UNKNOWN — health work never blocks sending.
7. Manual checks are rate-limited and concurrency-guarded (per-sender lock, TTL); triggered_by is restricted to `MANUAL` from the API.
8. API is RBAC-scoped (`integrations.connect` runs checks, `integrations.read` reads overview/history) and tenant-isolated.
9. Frontend: SenderDetail "Check Sender Health" flow with expandable per-check findings and a "How is this score calculated?" panel; WorkspaceSenders health column shows the score; health history visible.
10. Verification: new backend + frontend tests, legacy System-B regression, full non-Redis suite, lint, typecheck/build all green; `PHASE5_REPORT.md` records exact PASS/FAIL.

**STOP condition:** Phase 6 (warmup engine) must NOT be started after this report.

---

## 2. Context — What Existed Before Phase 5

Phase 4 delivered the Sender center with a **nullable** `health_status`/`health_score` and an explicit placeholder in the UI ("Health scoring is populated by the delivery-health engine in a later phase"). The Sender model already carried `health_status`, `health_score`, `last_health_check_at`, and a legacy System-B module `app/services/sender_health.py` (`evaluate`/`record_send_outcome`, used by the old warmup path) coexisted in the tree. There was no health evaluation, no DNS layer, no health history, and no scoring.

Phase 5 adds:
- A dedicated engine package (`app/services/sender_health_engine/`) with DNS resolver, GOOGLE provider adapter (MICROSOFT/SENDGRID/ZOHO/SMTP as extension points), nine checks, scoring, and an orchestrator that persists sanitized outcomes.
- `SenderHealthCheck` / `SenderHealthCheckResult` models + Alembic migration `20260916_04`.
- Three API endpoints under `/api/v1/senders/…` (run, overview, paginated history).
- Config for manual-check interval, lock TTL, and history retention.
- Frontend health flow in `SenderDetail` and health score in `WorkspaceSenders`.

---

## 3. Architecture

```
app/services/sender_health_engine/
├── types.py        CheckStatus/Severity/HealthStatus/Trigger, CHECK_TYPES, CheckResult, CheckContext
├── domain.py       extract_domain() — validates + normalizes the sender domain (IDNA, case)
├── dns.py          DnsResolver — TXT/SPF/DKIM/DMARC/MX against the system resolver; strict UNKNOWN on errors
├── providers.py    ProviderHealthAdapter base + GoogleHealthAdapter (extension point for others)
├── checks.py       9 check functions, deterministic order
├── scoring.py      WEIGHTS v1, thresholds (80/60), UNKNOWN_HANDLING, deterministic overrides
├── orchestrator.py SenderHealthService — run/latest/history, DB persistence, locking, rate limit, audit
└── __init__.py     SenderHealthService, SenderHealthError, build_health_overview_payload
```

Plumbing: `Sender.health_checks` relationship (`app/models/`), migration `20260916_04_sender_health.py`, three settings in `app/core/config.py` (`sender_health_manual_min_interval_seconds=60`, `sender_health_lock_ttl_seconds=300`, `health_history_retention_days=90`), response schemas in `app/schemas/sender_health.py`, routes in `app/api/senders.py`.

### Naming correction (important)
The workspace tree already contained a **legacy module** `app/services/sender_health.py` (System-B `SenderHealthService`) imported by `app/services/integrations.py`, `tests/test_sender_health_system_b.py`, and `tests/test_sender_quota_and_warmup.py`. A first draft placed the Phase 5 package at `app/services/sender_health/`, which **shadowed the module** and broke 17 legacy tests. The Phase 5 package was therefore renamed to `app/services/sender_health_engine/` and every internal import updated; legacy imports of `from app.services.sender_health import SenderHealthService` continue to resolve to the module. Post-rename the 13 System-B tests pass, and quota/warmup `warmup_send` tests pass.

---

## 4. Scoring Model `v1`

| Weighted check | Weight |
|---|---|
| PROVIDER_CONNECTION | 20 |
| DMARC | 20 |
| MAILBOX_STATUS | 15 |
| SPF | 15 |
| DKIM | 15 |
| DNS (MX/PTR/MTA) | 5 |
| SENDING_CONFIGURATION | 5 |
| SENDING_SIGNALS | 5 |

- **Status bands:** ≥80 HEALTHY · 60–79 WARNING · <60 CRITICAL.
- **Unknown handling (surfaced verbatim in the UI):** UNKNOWN / NOT_APPLICABLE checks are excluded and their weight redistributed proportionally among checks that produced a result — an unknown check is never treated as a zero.
- **Deterministic overrides:** any FAIL with severity CRITICAL forces CRITICAL; any other FAIL forces at most WARNING; no known checks ⇒ UNKNOWN (score `None`).
- The score explanation object (`version`, `weights`, `unknown_handling`, `thresholds`) is returned on every check so a score is never shown without its explanation. `DOMAIN` deliberately carries no weight — it gates the DNS checks but is not itself a signal about the sending setup.

### The nine checks
`PROVIDER_CONNECTION` (status + stored-credential presence), `MAILBOX_STATUS` (suspended/deleted/unavailable), `DOMAIN` (extractable, IDNA-normalized), `SPF` (count, validity, `all` term), `DKIM` (selector discovery + TXT), `DMARC` (record + policy; `p=none` → WARNING), `DNS` (MX presence), `SENDING_CONFIGURATION` (sending enabled + provider/credential prerequisites), `SENDING_SIGNALS` (reporting; **UNKNOWN** when no signal source is configured — never fabricated).

Adapted state: only `GOOGLE` (`GoogleHealthAdapter`) is implemented; `MICROSOFT`/`SENDGRID`/`ZOHO`/`SMTP` return NOT_APPLICABLE until their adapters land. No fabricated DNS, no inbox-placement claims (mirrored in the UI: `Domains.tsx` already states "Passing does not guarantee inbox placement").

---

## 5. API Contract

All under `/api/v1/senders`.

| Method | Path | Permission | Purpose |
|---|---|---|---|
| POST | `/{sender_id}/health-check` | `integrations.connect` | Run a MANUAL check, returns full `SenderHealthCheckOut` |
| GET | `/{sender_id}/health` | `integrations.read` | `SenderHealthOverviewOut` + latest check + summary |
| GET | `/{sender_id}/health/history?page=&page_size=` | `integrations.read` | Paginated `SenderHealthHistoryOut` |

- **Rate limiting:** a Redis-backed `RateLimitService` enforces the per-sender manual interval (`sender_health_manual_min_interval_seconds`); **fail-open only outside production** (a Redis outage never blocks sending in dev; in production it stays strict).
- **Concurrency guard:** per-sender lock with `sender_health_lock_ttl_seconds`; overlapping runs return 409 `HEALTH_CHECK_IN_PROGRESS`; a stale lock (past TTL) is stolen safely.
- **Error contract:** `SenderHealthError` → `{detail, code}` detail object; tested codes include `HEALTH_CHECK_RATE_LIMITED` (429), `HEALTH_CHECK_IN_PROGRESS` (409), invalid trigger (400), sender-not-found / cross-tenant (404).
- **Audit:** manual runs and blocking outcomes are `log_event`-audited; the API test asserts ≥2 audit actions for a completed manual check.
- **Retention:** `health_history_retention_days=90` bounds history (cleanup entry point in the orchestrator, wired into settings).

---

## 6. Frontend Integration

- `frontend/src/types/senders.ts` — Phase 5 type group (result/check/explanation/overview/history).
- `frontend/src/api/workspaceSenders.ts` — `runHealthCheck`, `getHealth`, `getHealthHistory`; **all Decimal-on-the-wire scores (JSON strings like `"92.0000000000"`) are normalized to numbers at the API boundary**; error extraction now unwraps the nested `{detail, code}` body so users see real messages.
- `frontend/src/pages/SenderDetail.tsx` — replaces the Phase 4 placeholder with a live **Health** card: status pill, score, last check, engine summary, **Check health** action, expandable per-check findings (status pill + score + summary + recommendation), a "How is this score calculated?" weight/threshold/unknown-handling panel, and a **Health history** table.
- `frontend/src/pages/WorkspaceSenders.tsx` — health column now also shows the numeric score beside the status pill.
- `frontend/src/styles.css` — added `status-crit` (FAIL/CRITICAL red pill), `.sender-health-cell`, `.health-explanation`, `.health-score`, `.health-recommendation`.
- No forbidden copy added anywhere ("guaranteed inbox placement" / "100% deliverability" absent — verified by grep).

---

## 7. Verification — Final Checklist (exact pass/fail)

| # | Check | Result |
|---|---|---|
| 1 | 9 checks covering provider/mailbox/DNS/config/signals | **PASS** — engine unit tests assert every check type (58 tests) |
| 2 | Versioned transparent scoring (`v1`, weights + thresholds + unknown rule in every response) | **PASS** — golden-run asserts weights `PROVIDER_CONNECTION=20`… and thresholds `{healthy:80, warning:60}` |
| 3 | UNKNOWN/NOT_APPLICABLE excluded, weight renormalized; all-unknown ⇒ UNKNOWN | **PASS** — renormalization tests + all-UNKNOWN ⇒ `None` score |
| 4 | CRITICAL-FAIL ⇒ CRITICAL; other FAIL ⇒ at most WARNING | **PASS** — override tests incl. REVOKED ⇒ CRITICAL |
| 5 | No secrets/tokens/credentials in logs, responses, or stored rows | **PASS** — secret-absence test in run payload + metadata holds only public DNS record info (DMARC tags, counts, statuses) |
| 6 | DNS/Redis outage fails gracefully to UNKNOWN | **PASS** — DNS-outage ⇒ UNKNOWN test; frontend renders "Unknown" |
| 7 | Rate limit + concurrency guard + MANUAL-only trigger | **PASS** — 429, 409, 400-invalid-trigger tests; stale-lock steal test |
| 8 | RBAC `integrations.connect`/`integrations.read`, tenant-isolated | **PASS** — 401/403/404 cross-tenant tests; overview/history RBAC |
| 9 | Frontend health flow + health column + history | **PASS** — SenderDetail 8 health-related cases (run/findings/explanation/history/error), WorkspaceSenders score column |
| 10 | Verification green | **PASS** — see counts below |

**Test counts (exact):**
- `tests/test_sender_health_engine.py` — **58 passed** (domain, DNS, all 9 checks, scoring thresholds/renormalization/overrides, orchestrator integration incl. rate-limit, 409, stale-lock steal, cross-tenant 404, catastrophic failure ⇒ recorded UNKNOWN row, secret absence).
- `tests/test_sender_health_api.py` — **13 passed** (401, RBAC 403/read-allowed, golden run 200 + 9 results, DB write, 429, 409, cross-tenant 404, overview + domain_authentication_summary, history pagination/newest-first, 422, redaction).
- Legacy regression: `test_sender_health_system_b.py` — **13 passed** (green again after the package rename).
- `test_sender_quota_and_warmup.py` + `test_integrations.py` (warmup/explicit-recipient scope) — **47 passed, 1 deselected**.
- **Full non-Redis backend suite:** `pytest tests --ignore=test_mailboxes.py --ignore=test_scheduler.py --ignore=test_campaign_sender_pool.py -k "not warmup_toggles_and_settings"` → **746 passed, 1 deselected, 3 warnings in 21m11s**. The 1 deselected (`test_warmup_toggles_and_settings_round_trip`) is the known environmental Redis failure. The 16 excluded file failures are the documented pre-existing Redis/OAuth-state suite (`test_mailboxes`, `test_scheduler`, `test_campaign_sender_pool`) — unrelated to Phase 5 and previously verified via `git stash`.
- **Frontend:** 10 files / **63 tests passed**; `npm run lint` clean; `npm run build` (`tsc -b && vite build`) clean.
- **Static checks:** `ruff` clean for engine + schemas + `app/api/senders.py` + new tests; `mypy --strict` clean ("Success: no issues found in 10 source files"). 6 ruff findings remain in `app/api/mailboxes.py`, `app/main.py`, `app/services/workspace_senders.py` — **pre-existing** (untracked working-tree files from Phases 3–4, untouched by Phase 5).
- **Alembic:** head `20260916_04`; migration is non-destructive (tables + indexes). Full chain cannot run on SQLite (pre-existing `20260825_12` limitation); model DDL verified instead.

---

## 8. Security Review (Phase 5)

| Area | Verdict |
|---|---|
| Secret handling | **PASS** — payloads built from sanitized model fields; metadata limited to public DNS tags/counts/statuses; provider adapters never touch credentials (only `credential_reference` presence is observed); API test asserts secrets absent |
| RBAC | **PASS** — run = `integrations.connect`, read = `integrations.read` |
| Tenant isolation | **PASS** — all queries scoped by tenant; cross-tenant returns 404 |
| Rate limiting | **PASS** — fail-open only outside production; strict in production |
| Concurrency | **PASS** — per-sender lock + TTL + stale-lock steal, 409 on overlap |
| Input validation | **PASS** — UUID path params; page/page_size bounds (422) |
| Error leakage | **PASS** — sanitized `error_code`/`error_message`; mapped HTTP codes, no stack traces |
| SSRF / DNS | **PASS** — domain extracted only from the sender email; no user-controlled URLs; resolver downgrades to UNKNOWN on NXDOMAIN/timeout |
| Audit | **PASS** — `log_event` on manual runs + notable outcomes; ≥2 audit actions per completed check |
| Migration | **PASS** — additive only; guarded; index-backed |

**Notable bugs found & fixed during development:** DMARC `p`-tag case-sensitivity (`_parse_dmarc` emits uppercase keys; `dmarc_check` now reads them case-correctly); `extract_domain` IDNA-encode preserved case so uppercase domains never matched the lowercase-only `DOMAIN_RE` (`.lower()` applied after decode); `NoNameservers.rcode()` typed safely; migration unused-variable removed.

---

## Files Touched (Phase 5)

**Backend**
- `backend/app/services/sender_health_engine/` — new engine package (types/domain/dns/providers/checks/scoring/orchestrator/`__init__`).
- `backend/app/models/entities.py` — `SenderHealthCheck` / `SenderHealthCheckResult` + `Sender.health_checks`.
- `backend/app/alembic/versions/20260916_04_sender_health.py` — head migration.
- `backend/app/core/config.py` — health settings (manual interval, lock TTL, retention) + `from_env()`.
- `backend/app/schemas/sender_health.py` — response schemas.
- `backend/app/api/senders.py` — 3 health endpoints, `_health_service`, `health_rate_limits`, `_http_from_health_error`.
- `backend/app/core/logging.py` — `_LOG_FIELDS` extended.
- `backend/tests/test_sender_health_engine.py` (58 tests) + `backend/tests/test_sender_health_api.py` (13 tests).

**Frontend**
- `frontend/src/types/senders.ts` — Phase 5 health types.
- `frontend/src/api/workspaceSenders.ts` — health API methods + Decimal→number normalization + nested-error unwrap.
- `frontend/src/pages/SenderDetail.tsx` + `SenderDetail.test.tsx` — live health card, findings, explanation panel, health history (health tests added).
- `frontend/src/pages/WorkspaceSenders.tsx` + `WorkspaceSenders.test.tsx` — score in health column (test added).
- `frontend/src/styles.css` — `status-crit` + health-card styles.

---

## Signed Off By

**Phase 5 complete.** No warmup-engine (Phase 6) work was initiated. Engine degrades gracefully to UNKNOWN without Redis/DNS, never fabricates scores, never surfaces secrets, and ships its scoring rules alongside every verdict. Report generated after the final green verification run (backend 746 passed / 1 deselected, frontend 63 passed, lint + tsc/build clean).

---

*End of Phase 5 report.*