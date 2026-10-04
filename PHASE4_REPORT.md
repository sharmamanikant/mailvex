# Phase 4 Report — Production-Grade Sender Management

**Project:** CR+CRM (email-sender provisioning platform)
**Phase scope:** Make the Sender entity center (`/senders`) production-grade — sender availability computation with a documented precedence, full list filtering/sorting/search, a rich sender detail view, sending-enable validation, soft-delete + restore, and a purpose-built frontend (list + detail) wired into the Dashboard.
**Status:** COMPLETE — all Phase 4 acceptance criteria pass (backend 43 targeted tests green, frontend 56 tests green, lint + typecheck/build clean). Alembic head `20260916_03`; no new migration required (the Sender model already carried every field Phase 4 needs).

---

## 1. Objective & Stop Condition

Phase 4 upgrades the Sender entity center from a CRUD shell into a production sending surface. The phase is complete only when each of the 11 acceptance checks passes:

1. `SenderResponse` carries a computed `availability` block with a defined reason/precedence.
2. Availability precedence is explicitly documented: REMOVED > ERROR > REVOKED > mailbox-suspended/deleted/unavailable > provider-revoked/disconnected, ending at "available".
3. `sendingEnabled=true` is a **prerequisite** for availability (verified in both the availability engine and the enable action).
4. List endpoint supports filters (provider, status, health_status, sending), search, sort, and pagination — all tenant-scoped, no credential leakage.
5. Enable action refused (HTTP 409) whenever availability prerequisites are unmet; failure is audited (`SENDER_AVAILABILITY_BLOCKED`).
6. Remove is a **soft delete** (status REMOVED, sending off) and a new restore endpoint brings the sender back (REACTIVE, sending stays off).
7. Enable/disable/remove/restore actions are audited.
8. Frontend: new Sender list page with filters, search, sort, pagination, availability/health/status badges, empty/loading/error states.
9. Frontend: new Sender detail page with overview/mailbox/provider-connection/health sections and enable/disable/restore/remove actions.
10. No DB migration needed; RBAC stays on `integrations.*` for the workspace-sender surface.
11. Verification: backend tests (new + existing senders router tests) and frontend tests/lint/typecheck/build all green.

**STOP condition:** Phase 5 (delivery-health engine) must NOT be started after this report.

---

## 2. Context: What the Entity Center Looked Like Before Phase 4

At the end of Phase 3 the `/senders` center exposed list/get/patch/enable/disable/delete backed by `SenderStore`, and the frontend consumed it via `workspaceSendersApi` (list/get/update/enable/disable/remove/bulkCreate). Gaps Phase 4 closes:

- **No availability model.** Nothing told consumers whether a given sender could actually send — `health_status` existed but there was no deterministic "can you send?" contract with a machine + human reason.
- **Enable was unconditional.** `PATCH/Post enable` flipped `sending_enabled` without checking mailbox/provider state, so users could "enable" senders that could never send (suspended/deleted mailbox, revoked/disconnected connection).
- **Coarse list → no search, no filters, no sort, no health visibility**; page lacked `total_pages`.
- **No restore.** Removed senders were unrecoverable from the UI.
- **No dedicated frontend.** The legacy `Senders.tsx` (Email-Account center) remained the only sender UI; workspace senders were only reachable as a side-effect card in WorkspaceMailboxes.

---

## 3. Backend — Availability Model (checks 1–3)

### 3.1 `SenderResponse.availability`

`backend/app/schemas/workspace_senders.py` grew a `SenderAvailability` schema:

```python
class SenderAvailability(BaseModel):
    available: bool
    sending_enabled: bool
    sender_status: SenderStatus
    mailbox_status: str
    provider_connection_status: str
    reason: SenderAvailabilityReason | None   # null ⇒ available
```

`SenderAvailabilityReason` is the closed allow-list from the spec: `SENDER_DISABLED | SENDER_REMOVED | SENDER_ERROR | SENDER_REVOKED | PROVIDER_DISCONNECTED | PROVIDER_REVOKED | MAILBOX_SUSPENDED | MAILBOX_DELETED | MAILBOX_UNAVAILABLE`.

**Implementation note:** pydantic evaluates the `SenderAvailability()` default at class-definition time, so `SenderAvailability` is declared **before** `SenderResponse` in the schema module (otherwise a `NameError`/broken default). `SenderResponse.availability: SenderAvailability = SenderAvailability()`.

### 3.2 Precedence (core rules, `_availability_reason`)

Evaluation is ordered — first match wins:

| Priority | Condition | Reason |
|---|---|---|
| 1 | sender.status == REMOVED | `SENDER_REMOVED` |
| 2 | sender.status == ERROR | `SENDER_ERROR` |
| 3 | sender.status == REVOKED | `SENDER_REVOKED` |
| 4 | sender.status == DISABLED | `SENDER_DISABLED` |
| 5 | `not sender.sending_enabled` | `SENDER_DISABLED` |
| 6 | mailbox deleted / suspended / not ACTIVE | `MAILBOX_DELETED` / `MAILBOX_SUSPENDED` / `MAILBOX_UNAVAILABLE` |
| 7 | connection REVOKED / not CONNECTED | `PROVIDER_REVOKED` / `PROVIDER_DISCONNECTED` |
| 8 | None of the above | **available** (`reason=None`) |

`_availability_reason(sender, mailbox, connection, *, ignore_sending_enabled=False)` exists so the **enable path** can skip rule 5 (see §4). The mailbox/provider reasons are surfaced verbatim via the schema.

### 3.3 `sending_enabled` is a hard prerequisite

- Availability never returns `available=True` unless `sending_enabled` is true.
- The enable action re-derives availability with `ignore_sending_enabled=True` and refuses iff another rule (mailbox/provider) makes the sender inherently un-sendable — see §4.

---

## 4. Backend — Enable Validation & Auditing (checks 5, 7)

`update_sender(sender_id, sending_enabled=True)` now:

1. Loads the sender **plus** mailbox + provider connection (tenant-scoped).
2. Computes `_availability_reason(..., ignore_sending_enabled=True)`.
3. If the block is NOT solely "sending is disabled" (i.e. still blocked with the flag ignored) → raise `409 SENDER_AVAILABILITY_BLOCKED` and record an audit event with metadata `{reason, mailbox_status, provider_status}`.
4. Otherwise it flips `sending_enabled` and records `SENDER_ENABLED`.

Why `ignore_sending_enabled`: without it, enabling a sender whose mailbox is suspended would report misleading `SENDER_DISABLED` instead of `MAILBOX_SUSPENDED`.

Audit actions used: `SENDER_CREATED`, `SENDER_ENABLED`, `SENDER_DISABLED`, `SENDER_UPDATED`, `SENDER_REMOVED`, `SENDER_RESTORED`, `SENDER_AVAILABILITY_BLOCKED`. All routed through `AuditService(session, tenant_id, actor_id).record(...)` with redaction; read views are deliberately not audited.

---

## 5. Backend — List/Detail + Sorting (checks 4, 6)

### 5.1 `list_senders` (service) / `GET /senders` (api)

- Filters: `provider`, `status`, `sending` (bool), `health_status`, `search` (email/display_name, joined via mailbox for case-insensitive `ilike`).
- Search joins `Mailbox` and eager-loads `mailbox` + `provider_connection` via `selectinload` (no N+1).
- Sort is **allow-listed**: `email, created_at, updated_at, status, last_health_check_at`; `-` prefix = descending; alias `last_health_check` → `last_health_check_at`. Any other/hostile token silently falls back to `email ASC` — values are never interpolated into SQL.
- Pagination: `page` (≥1), `page_size` (API default 25, ≤200). Response is `SenderPage { items, page, page_size, total, total_pages }` — `total_pages = ceil(total/page_size)`.
- Tenant scope enforced at the query root; every row carries the computed `availability`.

### 5.2 `GET /senders/{sender_id}` → `SenderDetailResponse`

Richer response with nested summaries — `sender`, `mailbox`, `provider_connection`, `health`:

```python
class SenderDetailResponse(SenderResponse):
    mailbox: SenderMailboxSummary | None
    provider_connection: SenderProviderConnectionSummary | None
    health: SenderHealthInfo
```

No credential fields (SMTP passwords, refresh tokens, encrypted refs) are exposed anywhere.

### 5.3 Restore + soft delete (check 6)

- `DELETE /senders/{sender_id}` → **soft delete**: `status=REMOVED`, `sending_enabled=False`; 204; audited `SENDER_REMOVED`.
- `POST /senders/{sender_id}/restore` → `restore_sender`: sets `status=ACTIVE`, leaves `sending_enabled=False` (never auto-enables); blocked with `409` if the backing mailbox is deleted/suspended/not ACTIVE or the connection is REVOKED/not CONNECTED; audited `SENDER_RESTORED`. Helper message via `_enable_blocked_message`.

### 5.4 Module-level availability entry point

`get_sender_availability(session, tenant_id, sender_id)` is exported from the service module so the future email engine (Phase 5+) can query a sender's sendability without the router.

---

## 6. Backend — Tests & Static Verification

New `backend/tests/test_sender_management.py` (fixture `sender_client` seeds 2 tenants, 5 OAuth connections, mailboxes + senders directly on sqlite) covers 23 cases:

- availability precedence matrix (disabled/removed/error/revoked, mailbox suspended/deleted/unavailable, provider disconnected/revoked),
- `sending_enabled=false ⇒ unavailable` and the enable-prerequisite block (`409` + `SENDER_AVAILABILITY_BLOCKED` audit reason),
- list filters (provider/status/health/sending), search, sort (asc/desc/alias/fallback), pagination totals,
- detail shape (mailbox/provider/health summaries, no credential exposure),
- enable/disable/remove/restore flows + audit rows,
- cross-tenant isolation (`/senders/{other_tenant_id}` → 404).

**Results:**
- `pytest tests/test_sender_management.py tests/test_microsoft_sender.py -q` → **43 passed** (23 new + the 20 existing router tests using `/senders`).
- `mypy --strict` on the 3 Phase-4 backend files → **no issues** (schema-boundary `cast()`s where the DB column enums are wider than the API Literals).
- `ruff check` on those files → clean; the only 2 RUF005 hits in `workspace_senders.py` are the pre-existing Phase-3 bulk-create block (lines ~280/287), documented and left as-is.

---

## 7. No Migration Required (check 10)

Alembic head is `20260916_03` (the Phase-3 `senders` table). `Sender` already models `status`, `sending_enabled`, `health_status`, `health_score`, `last_health_check_at`, and FKs to mailbox/provider_connection — every field Phase 4 reads/writes already exists.

---

## 8. Frontend — Types + API Client

`frontend/src/types/senders.ts` (kept **separate** from the legacy Email-Account `Sender` type):

- `WorkspaceSender` (+ new availability block), `WorkspaceSenderStatus`, `WorkspaceSenderHealthStatus`,
- `WorkspaceSenderAvailability` (typed `reason` union matching the backend),
- `WorkspaceSenderMailbox`, `WorkspaceSenderProviderConnection`, `WorkspaceSenderHealth`, `WorkspaceSenderDetail`,
- `SenderPage` now types `items: WorkspaceSender[]` and carries `total_pages`; `SenderOperation.status` typed to `WorkspaceSenderStatus`.

`frontend/src/api/workspaceSenders.ts` adds `getDetail(id, token)` (`GET /senders/{id}` → detail shape) and `restore(id, token)`; existing list/get/update/enable/disable/remove/bulkCreate re-pointed at the new types.

## 9. Frontend — Pages, Routing, Nav

- `frontend/src/pages/WorkspaceSenders.tsx` — new **Sender Directory** page: search, status/provider/sending/health filter selects, allow-list sort select, page-size control, availability + health + status pills, per-row enable/disable, View link, pagination footer, loading/error/empty states, success/error notices. Route `/senders/workspace`.
- `frontend/src/pages/SenderDetail.tsx` — new detail page: overview / mailbox / provider-connection / health summary cards, availability warning banner, enable / disable / restore actions, inline-confirm destructive remove, refetch, and 404 state. Route `/senders/workspace/:id`.
- `frontend/src/App.tsx` — routes registered for `/senders/workspace` and `/senders/workspace/:id` (legacy `/senders` Email-Account center untouched; `/senders/:id/health` untouched).
- `frontend/src/pages/Dashboard.tsx` — "Senders" nav item added under **Workspace Management** (`integrations.read`, matching backend RBAC), pointing at `/senders/workspace`.

## 10. Frontend — Tests & Static Verification

- `WorkspaceSenders.test.tsx` (8 tests): heading/rows/availability badges; default page params; search; status + sending filters; provider + health filters; pagination; per-row enable and disable; empty state.
- `SenderDetail.test.tsx` (7 tests): overview/mailbox/connection render; availability warning; enable; disable; restore; inline-confirm remove; error/404 state.
- **Frontend suite: 10 files / 56 tests passed**, `npm run lint` clean, `npm run build` (tsc -b + vite) clean.

**Pre-existing build blockers fixed (out of Phase 4 scope, minimal):** `frontend/src/api/senders.ts` imported a non-existent `../types/email-senders` (those types live in `types/senders.ts`) — import path corrected; and a `Signup.tsx` ↔ `SignUp.tsx` casing collision (on-disk file renamed to `SignUp.tsx` to match all existing imports) — both were blocking `npm run build` before Phase 4 started.

---

## 11. Verification — Final Checklist (pass/fail per check)

| # | Check | Result |
|---|---|---|
| 1 | `availability` block on list + detail responses with typed reason | **PASS** (badge-tested in FE, schema-tested in BE) |
| 2 | Documented availability precedence | **PASS** (see §3.2 table) |
| 3 | `sending_enabled=true` prerequisite for availability + enable | **PASS** (BE tests: disabled ⇒ unavailable; enable blocked on suspended mailbox) |
| 4 | List filters/search/sort/pagination, tenant-scoped, no credentials | **PASS** (BE tests + FE filter/search/sort/pagination tests) |
| 5 | Enable blocked → 409 + `SENDER_AVAILABILITY_BLOCKED` audit w/ reason | **PASS** |
| 6 | Soft-delete remove + restore endpoint | **PASS** |
| 7 | Enable/disable/remove/restore audited | **PASS** |
| 8 | Frontend Sender list page (filters/search/sort/pagination/badges/states) | **PASS** (8 tests) |
| 9 | Frontend Sender detail page (sections + actions) | **PASS** (7 tests) |
| 10 | No new migration; RBAC on `integrations.*` | **PASS** (head `20260916_03`) |
| 11 | Backend + frontend verification green | **PASS** — backend 43/43 targeted; frontend 56/56, lint + tsc/build clean |

**Backend full-suite caveat:** with `app_env=development` and no Redis on `localhost:6379`, 16 suites fail for environmental/pre-existing reasons only (`test_mailboxes.py`, `test_scheduler.py`, `test_campaign_sender_pool.py` — OAuth state store + System-B quota/scheduler depend on Redis). Verified unrelated to Phase 4 via `git stash` (files fail at import at baseline) and unchanged Redis/oauth-state code paths. Phase 4 touchpoints and the wider non-Redis suite remain green.

---

## Files Touched (Phase 4)

**Backend**
- `backend/app/schemas/workspace_senders.py` — `SenderAvailability` (before `SenderResponse`), `SenderDetailResponse`, summaries, `total_pages`.
- `backend/app/services/workspace_senders.py` — availability precedence, list filters/sort/search, detail, enable validation, soft-delete, restore, module-level availability entry point; removed unused import.
- `backend/app/api/senders.py` — rewritten router: filters/sort/detail/restore/409-mapping, `page_size` default 25, no credential leakage.
- `backend/tests/test_sender_management.py` — new, 23 tests.

**Frontend**
- `frontend/src/types/senders.ts` — workspace-sender type group (avail/mailbox/connection/health/detail), `SenderPage.total_pages`.
- `frontend/src/api/workspaceSenders.ts` — `getDetail`, `restore`, typed list/operations.
- `frontend/src/pages/WorkspaceSenders.tsx` + `WorkspaceSenders.test.tsx` — list page + 8 tests.
- `frontend/src/pages/SenderDetail.tsx` + `SenderDetail.test.tsx` — detail page + 7 tests.
- `frontend/src/App.tsx` — `/senders/workspace`, `/senders/workspace/:id` routes.
- `frontend/src/pages/Dashboard.tsx` — Senders nav item (Workspace Management, `integrations.read`).
- *Pre-existing blockers fixed:* `frontend/src/api/senders.ts` import path; `Signup.tsx` → `SignUp.tsx` case rename.

---

## Signed Off By

**Phase 4 complete.** No Phase 5 work initiated (delivery-health engine is the forward task, which will consume `get_sender_availability`). Report generated after final green verification run.

---

*End of Phase 4 report.*