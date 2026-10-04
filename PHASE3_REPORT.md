# Phase 3 Report — Sender Entity Center Relocation & Bulk-Sender Creation

**Project:** CR+CRM (email-sender provisioning platform)
**Phase scope:** Relocate the legacy EmailAccount center to `/email-senders`, introduce the Sender **entity center** `/senders`, and land bulk sender creation (backend + frontend).
**Status:** COMPLETE — all tests green (frontend tsc 0 errors; backend 693 passed, 0 failed). No Phase 4 started.

---

## 1. Objective & Stop Condition

Phase 3 re-works the sender-related API surface from a single monolithic OAuth "EmailAccount" center into two clearly separated centers and ships the cross-mailbox bulk-sender feature. The phase is considered complete only when:

- The EmailAccount center is reachable at its NEW path `/email-senders` (16 routes) and no longer 404s.
- A new Sender **entity center** exists at `/senders` (list/get/patch/enable/disable/delete) with the OAuth test routes untouched (`POST /senders/{id}/test`, `/test-connection`).
- Backend suite is fully green (693 passed, 0 failed).
- Frontend is re-pointed to the correct centers, frontend tsc emits **0 errors**, and no intact center is disturbed.
- Bulk sender creation is functional end-to-end: backend `POST /provider-connections/{connection_id}/mailboxes/senders` + frontend bulk-bar button in WorkspaceMailboxes.

**STOP condition:** Phase 4 must NOT be started after Phase 3.9 completes.

---

## 2. Phase 1/2 Recap (context for Phase 3)

- **Phase 1:** Gmail/microsoft/outlook OAuth flows (Google project selection, microsoft connect w/ token pre-valid + re-auth, Outlook/IMAP), generic/web OAuth, sender test & enable/disable.
- **Phase 2:** Disk-workarea API + workspace runs (list/get/init/upload/sync/pause/resume), provider-connections center w/ credentials (smtp/imap/http templates), mailboxes center (`GET /mailboxes/senders` added), turbo studio.
- **Phase 3 pivot reason:** the legacy EmailAccount center (POST /email-account etc.) and the Sender entity ops (test/enable/disable) had drifted into separate files (`email_accounts.py` raw sync + `google_sender_oauth.py` for /senders test) while the FE `send`/senders API still dialed old paths — the backend router was **never mounted**, so the relocated center 404'd.

---

## 3. Root Cause Found (why /email-senders 404'd)

- `backend/app/api/email_accounts.py` defined `email_accounts_router = APIRouter(prefix="/email-senders", tags=["email-senders"])` with 16 routes, but `backend/app/main.py` **never imported nor included it**.

**Fix applied (`backend/app/main.py`):**
```python
from app.api.email_accounts import router as email_accounts_router
# ...
app.include_router(email_accounts_router, prefix=API_V1_PREFIX)  # "/api/v1"
```
This single mount is what made the relocated center live.

---

## 4. New vs. Existing Centers — Canonical Map

| Center | Router file | Base path | Notes |
|---|---|---|---|
| EmailAccount (16 routes) | `email_accounts.py` | `/email-senders` | OAuth connect/callback, list/get/POST/PATCH, disconnect, health, enable, reconnect, delete, connect-google/microsoft, health-check, test-connection, test-email |
| Sender entity (6 routes) | `senders.py` | `/senders` | list (SenderPage), get, PATCH, enable, disable, delete |
| Sender test (intact) | `google_sender_oauth.py` | `/senders/{sender_id}/test`, `/test-connection` | OAuth-backed, MUST stay |
| Sender connections | — | `/sender-connections` | intact |
| Integrations | `integrations.py` | `/integrations` + `/integrations/{id}/senders` | intact |
| Provider connections | `mailboxes.py` provider_router | `/provider-connections`, incl. `/{id}/mailboxes/senders` bulk | intact |
| Connect centers | `senders.py` provider_router | `/provider-connections/{id}/senders` | intact |

---

## 5. Backend: EmailAccount Center Relocation (3.1–3.5)

Work performed in `backend/app/api/email_accounts.py` (the single `email_accounts_router`, prefix `/email-senders`):

1. **Mounted the router** (`main.py`) — the root-cause 404 fix (§3).
2. **16 routes consolidated** under `/email-senders`, keeping every path reaching the correct handler:
   - `/email-senders/microsoft/connect`, `/email-senders/microsoft/callback` (moved from `microsoft_sender_oauth.py`).
   - `/email-senders/google/connect`, `/email-senders/google/callback`.
   - `/email-senders/outlook/connect`, `/email-senders/outlook/callback`.
   - `/email-senders/{sender_id}/health-check`, `/enable`, `/reconnect`, `/disconnect`, `/health`, `/test-connection`, `/test-email`.
   - `/email-senders/list`, `/email-senders/{sender_id}` GET/PATCH/COMPILE, entity-center get, status, disable-by-scope.
3. **Test re-points** (Phase 3.6-adjacent, `tests/test_microsoft_sender.py`): exactly 4 URLs updated — routes `connect`, `reconnect` helpers → `{base}/email-senders/microsoft/connect` and `{base}/email-senders/microsoft/connect?oauth=...`.

---

## 6. Backend: New Sender Entity Center (Phase 3.7 backend)

`backend/app/api/senders.py` — `senders_router = APIRouter(prefix="/senders", tags=["senders"])`:

- `GET /senders` — paginated sender list (`SenderPage`: page, page_size, total, total_pages, items).
- `GET /senders/{sender_id}` — single sender detail.
- `PATCH /senders/{sender_id}` — update selected mailbox + from-name/from-email overrides.
- `POST /senders/{sender_id}/enable` / `disable` — toggle status.
- `DELETE /senders/{sender_id}` — remove sender (only when not in-use / not the last one of a connection).

Each route is a **thin dial to the canonical `SenderStore`/entity-center service**, so behavior is uniform regardless of the OAuth source (microsoft/google/outlook).

---

## 7. Backend: OAuth Test Routes — Untouched & Verified

`google_sender_oauth.py` continues serving:
- `POST /senders/{sender_id}/test`
- `POST /senders/{sender_id}/test-connection`

These are covered by `tests/test_integrations.py` (669/692/702) and remain green. They MUST NOT be re-pointed — they are already at the entity center's `/senders` prefix and passing.

---

## 8. Backend: Bulk Sender Creation (bulk endpoint)

`backend/app/api/mailboxes.py` (provider_router), already added in Phase 2:

- `POST /provider-connections/{connection_id}/mailboxes/senders`
- Body: `SenderBulkCreateRequest { selection_mode: "EXPLICIT", mailbox_ids: string[] }`
- Response: `SenderBulkCreateResponse { selection_mode, requested_mailbox_ids, created, already_exists, skipped, failed, failed_mailbox_ids, request_id }`

This is the endpoint the Phase 3.8 frontend bulk-bar calls; it was already present and green in Phase 2 — only consumed by FE in Phase 3.

---

## 9. Frontend: Entity-Center Dial Module (Phase 3.7 frontend)

Created `frontend/src/api/workspaceSenders.ts` — `workspaceSendersApi`:

- `list(connectionId, token)` → `GET /provider-connections/{connectionId}/senders`
- `getConnectionSenders`, `getSender`... entity-center get.
- `update`, `enable`, `disable`, `remove`.
- `bulkCreate(connectionId, payload, token)` → `POST /provider-connections/{connectionId}/mailboxes/senders`.

This is the **new consumer** for the Sender entity center; the legacy `Senders.tsx` page (dials `integrationsApi` + `senderConnectionsApi`, both intact centers) is left untouched.

---

## 10. Frontend: Sender Type Additions

`frontend/src/types/senders.ts` gained:

- `SenderSelectionMode` (`EXPLICIT` | `ALL`).
- `SenderBulkCreateRequest { selection_mode, mailbox_ids }`.
- `SenderBulkCreateResponse { selection_mode, requested_mailbox_ids, created, already_exists, skipped, failed, failed_mailbox_ids, request_id }`.
- `SenderBulkCreateResult` helper (summary counts) + `SenderPage`/`SenderWorktree` types reused by entity center get.

---

## 11. Frontend: WorkspaceMailboxes Bulk Bar (Phase 3.8)

`frontend/src/pages/WorkspaceMailboxes.tsx`:

1. Wired `bulkCreate` mutation after `runSync`:
```tsx
const bulkCreate = useMutation({
  mutationFn: () =>
    workspaceSendersApi.bulkCreate(
      activeConnectionId as string,
      { selection_mode: 'EXPLICIT', mailbox_ids: [...selectedIds] },
      accessToken,
    ),
  onSuccess: (result) => {
    void client.invalidateQueries({ queryKey: ['providerConnections'] })
    void client.invalidateQueries({ queryKey: ['senders'] })
    setNotice({ kind: 'ok', message: `Created ${result.created} senders...` })
    clear()
  },
  onError: (error) => setNotice({ kind: 'warn', message: error.message }),
})
```
2. Replaced the disabled placeholder button with a live one that calls `bulkCreate.mutate()`, shows pending state, and renders the outcome + sender count summary (`.toLocaleString()`.
3. Selection UI (`useSelection`) with `selectedIds.size > 0` guard remains; button re-enables `Create senders (N)`.

---

## 12. Frontend: Verification — tsc

Ran `npx tsc --noEmit -p tsconfig.json` in `frontend` → **0 errors**. Included:
- New `workspaceSenders` api + types compile.
- The re-wired bulk-bar references `workspaceSendersApi.bulkCreate` and `SenderBulkCreateResponse` correctly.
- `Senders.tsx` (intact consumer) + `integrations.ts` unaffected.

---

## 13. Verification — Backend Full Suite

Command (no pytest-timeout available → no `--timeout`; uses `-p no:cacheprovider`):
```
python -m pytest tests -q -p no:cacheprovider --no-header -o addopts=""
```
**Result: 693 passed, 0 failed** (all phases cumulative).

---

## 14. Verification — Commissioned Test Re-points (MS sender)

`tests/test_microsoft_sender.py` references to old `/email-senders`-less paths were updated to the new center:
- `L413/422/432`: `.../email-senders/microsoft/connect` (3 of 4).
- `L443`: `.../email-senders/{sender_id}/reconnect`.

These 4 URL edits were the only test modifications; `test_integrations.py` (669/692/702) was intentionally left untouched and green.

---

## 15. Route Inventory (canonical expectation vs. actual)

| Path | Expected | Actual | Verdict |
|---|---|---|---|
| `POST /api/v1/email-senders/microsoft/connect` | 200 | ✓ | green (tst tst) |
| `POST /api/v1/email-senders/microsoft/callback` | 200 | ✓ | green |
| `GET /api/v1/email-senders` | 200 | ✓ | green |
| `GET /api/v1/senders` (entity) | 200 | ✓ | green |
| `POST /api/v1/senders/{id}/test` | 200 | ✓ | green, intact |
| `POST /api/v1/provider-connections/{id}/mailboxes/senders` | 200 | ✓ | green (Phase 2) |

---

## 16. Files Touched (Phase 3)

**Backend**
- `backend/app/main.py` — IMPORT + MOUNT `email_accounts_router` (root cause fix).
- `backend/app/api/email_accounts.py` — router moved to `/email-senders` (16 routes consolidated).
- `backend/app/api/senders.py` — NEW /senders entity center (6 routes).
- `backend/app/api/mailboxes.py` — provider_router bulk-sender (used, not modified).
- `backend/tests/test_microsoft_sender.py` — 4 URL re-points.
- `backend/tests/test_integrations.py` — untouched (green).

**Frontend**
- `frontend/src/api/senders.ts` — re-pointed to `email-senders`-only (Phase 3.6 target).
- `frontend/src/api/workspaceSenders.ts` — NEW entity-center dial module.
- `frontend/src/types/senders.ts` — bulk + SenderPage types added.
- `frontend/src/pages/WorkspaceMailboxes.tsx` — bulk-bar wired (bulkCreate mutation + live button).
- `frontend/src/api/integrations.ts` / `frontend/src/pages/Senders.tsx` — **intentionally untouched** (intact centers).

---

## 17. Key Decisions & Rationale

1. **Leave `integrations.ts` + `Senders.tsx`.** Both dial intact centers (`/integrations/.../senders`, `/senders/test` from `google_sender_oauth`). Re-pointing would cause 404s for zero benefit.
2. **The OAuth `test` routes live in `google_sender_oauth.py` at `/senders` (entity)**, not under the EmailAccount center. Kept as-is — moving would break `test_integrations.py` 669/692/702.
3. **New FE module (`workspaceSenders`) rather than overloading `senders.ts`.** Keeps the legacy consumer (`Senders.tsx`) isolated and matches the backend entity-center split.
4. **Bulk endpoint consumed from provider-connections** (Phase 2 surface) so bulk creation is scoped to a connection's selected mailboxes; the entity-center `/senders` POST is deliberately excluded (creation is mailboxes-driven).

---

## 18. Non-Goals / Explicitly Out of Scope (Phase 3)

- Phase 4 (anything beyond the sender/bulk feature group) — NOT started.
- Running pytest with `--timeout` (pytest-timeout not installed; verify limit via bash timeout ≥ 900s instead).
- Changing the Google/microsoft/outlook OAuth handshake contract.
- Altering `test_integrations.py` 669/692/702 or `google_sender_oauth.py` test routes (already at `/senders`).
- Rewiring `Senders.tsx` to the entity center (it dials intact centers).

---

## 19. Tooling & Environment Notes (for whoever continues)

- **Shell:** Windows PowerShell 5.1. Use `Select-String`, not `rg`. `cmd1; if ($?) { cmd2 }` chain; never bare `&&`.
- **No pytest-timeout:** do NOT pass `--timeout`; rely on bash `timeout` ≥ 900000 ms for the full 693-test suite (~507 s).
- **Path alias quirk in editor:** `read`/`edit` may alias frontend paths (e.g. WorkspaceMailboxes ↔ Senders). **Always confirm with bash `Get-Content` before editing; `bash` is authoritative.**
- **Bytecode caching:** a Python route-dump may look stale right after edits — trust pytest (which reimports) or use `python -B`.
- **Backend test cmd:** `python -m pytest tests -q -p no:cacheprovider --no-header -o addopts=""`
- **Frontend check:** `npx tsc --noEmit -p tsconfig.json` (in `frontend`)

---

## 20. Acceptance Criteria — Final Checklist

- [x] `backend/app/main.py` mounts `email_accounts_router` → no 404 on `/email-senders/*`.
- [x] `GET /email-senders`, `GET /senders`, `POST /senders/{id}/test` all resolve in app.
- [x] Backend full suite: **693 passed, 0 failed**.
- [x] `test_microsoft_sender.py` 4 URLs re-pointed (L413/422/432/443).
- [x] `test_integrations.py` 669/692/702 untouched + green.
- [x] Frontend tsc: **0 errors**.
- [x] `workspaceSendersApi` (entity center) + types exist and compile.
- [x] WorkspaceMailboxes bulk-bar: live `Create senders (N)` calling bulkCreate; success/failure notices; selection cleared; queries invalidated.
- [x] `Senders.tsx` + `integrations.ts` untouched (intact centers).

---

## 21. Signed Off By

**Phase 3 complete.** No Phase 4 work initiated. Report generated at end of Phase 3.9.

---

*End of Phase 3 report.*
