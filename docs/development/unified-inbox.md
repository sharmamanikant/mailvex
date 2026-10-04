# Unified Inbox & Reply Management (Phase 17)

A three-pane `/inbox` that pulls replies from connected sender mailboxes, stores them durably, and lets a user reply manually or with an **AI draft that is never sent automatically**.

## Design principles

- **Durable, de-duplicated storage.** Replies sync from a provider into `email_threads` / `email_messages`. Messages are de-duplicated on `(tenant_id, external_message_id, provider)`, so re-syncing never creates duplicates.
- **Conservative association — never guess.** An inbound reply is linked to a contact **only** when exactly **one** contact matches the sender's email; it is linked to a campaign only when exactly one campaign was sent to that contact through that sender (a unique `Message` from that `sender_id` + `contact_id` + `campaign_id`). Anything less confident stays `match_status = UNMATCHED` with `contact_id`/`campaign_id` null.
- **AI never auto-sends.** `generate_reply` only produces a `DRAFT` row. The user must review, edit, and approve, and only the explicit `send` call actually transmits — with the exact subject/body passed by the caller.
- **Compliance gate on replies.** Replies are blocked (403) when the recipient is on the suppression list, and when the sender is `DISABLED`/`DISCONNECTED`/`SUSPENDED`/`REAUTH_REQUIRED`.

## Provider abstraction

`EmailProviderInterface` (`backend/app/providers/base.py`) gained non-abstract **default inert inbox methods** plus a class attribute:

- `supports_inbox: bool = False` — used to fail fast when a provider cannot read a mailbox.
- `list_threads()`, `get_messages(thread_id)`, `sync_messages(max_results)` default to `[]`.
- Dataclasses `ProviderThread` and `ProviderInboxMessage` carry the normalized shape for incoming mail.

`SMTP` inherits the inert defaults and **does not** pretend to sync — `InboxService.sync_sender` raises `InboxNotSupportedError` for it. `Gmail`, `Microsoft`, and `Mock` set `supports_inbox = True` and implement real adapters (Gmail uses the existing `gmail.readonly` scope; Microsoft added `Mail.Read` to its scopes; `Mock` exposes a seeded `inbox` plus `fail_sync` for tests).

## Models

Added in `backend/app/models/entities.py` (`EmailThread`/`EmailMessage`/`AIReplyDraft`), migrated by `backend/app/alembic/versions/20260830_24_inbox.py` (revision `20260830_24`, `down_revision = "20260830_23"`):

| Table | Notes |
| --- | --- |
| `email_threads` | `status` in `UNREAD/READ/REPLIED/ARCHIVED/REQUIRES_ACTION`; association `contact_id`/`campaign_id`; `match_status` `MATCHED/UNMATCHED`; unique `(tenant_id, sender_id, external_thread_id)` |
| `email_messages` | `direction` `INBOUND/OUTBOUND`; optional `in_reply_to`/`references`; unique `(tenant_id, external_message_id)` |
| `ai_reply_drafts` | `status` `DRAFT/APPROVED/SENT/FAILED`; provider + token counts; `approved_at` |

`external_thread_id` / `external_message_id` are **external provider IDs**, not relational foreign keys (they are in the metadata test's `non_relational_ids` allowlist, like the pre-existing `provider_thread_id`/`provider_message_id`).

## Services

- `backend/app/services/inbox.py` — `InboxService(session, tenant_id, actor_id=None, provider_factory=None)`. `provider_factory(sender)` is optional (used by tests); it defaults to `SenderService.provider(sender)`. Provides `sync_sender`, `list_threads` (page/status/sender filters), `get_thread`, `set_status`, and `recipient_context` (name/company/designation/campaign/last contact). A `_cmp_dt` helper normalizes aware/naive datetimes for safe comparisons across SQLite (naive) and Postgres.
- `backend/app/services/inbox_reply.py` — `InboxReplyService` for `draft` (AI → `DRAFT`, subject prefixed `Re: `), `approve`, and `send` (builds `In-Reply-To`/`References`, sends through the provider with the exact subject/body, appends an OUTBOUND `EmailMessage`, sets thread `REPLIED`, marks draft `SENT`). `_check_can_reply` raises `InboxReplyBlockedError` for suppressed recipients or unavailable senders; missing subject/body raises `InboxReplyError`.

## API (`/api/v1/inbox`, all gated by `analytics.read`)

- `GET /inbox/threads` — paginated list (`page`, `page_size`, optional `status`, `sender_id`).
- `GET /inbox/threads/{id}` — detail incl. `messages[]`, `recipient` context, `campaign_name`.
- `POST /inbox/threads/{id}/status` — set `UNREAD/READ/REPLIED/ARCHIVED/REQUIRES_ACTION`.
- `POST /inbox/sync` — `{ "sender_id" }`; returns `{ new_threads, new_messages }`.
- `POST /inbox/threads/{id}/reply/draft` — AI drafted body (never sent).
- `POST /inbox/threads/{id}/reply/approve` — `{ draft_id }` → sets `APPROVED`.
- `POST /inbox/threads/{id}/reply` — send `{ subject, body, draft_id? }`.

Error mapping (`app/api/inbox.py` `_error`): not-found → 404, `InboxReplyBlockedError` → 403, `InboxError`/`InboxReplyError`/`InboxNotSupportedError` → 400, `InboxSyncError` → 502.

## Frontend

- `frontend/src/types/inbox.ts`, `frontend/src/api/inbox.ts` (per-module `request<T>` pattern).
- `frontend/src/pages/Inbox.tsx` — three-pane layout: **thread list** (status/sender filters, per-sender "Sync replies"), **conversation** (message history + reply composer with subject/body and **AI draft** + **Send reply** buttons), **recipient context** (Name/Company/Designation/Campaign/Last contact). Sync is exposed per connected sender; AI draft pre-fills the composer for review.
- Route `/inbox` registered in `App.tsx`, nav item in `Dashboard.tsx`, styles in `styles.css` (`.inbox-layout.tri-pane`, `.recipient-panel`, `.reply-composer`, etc.).

## Tests

- `backend/tests/test_inbox.py` — 19 tests: sync thread/message creation, dedup on re-sync, thread separation, provider failure & SMTP-unsupported surfacing, MATCHED/UNMATCHED association, ambiguous-campaign handling, status transitions, recipient context, AI draft never auto-sends, manual reply sends + marks `REPLIED`, suppression-blocked replies, empty-content rejection, tenant isolation, API auth (401), list/get/set-status.
- `frontend/src/pages/Inbox.test.tsx` — 2 tests: heading/thread-list render, selecting a thread opens conversation + recipient panes.
- `tests/test_database_metadata.py` — include `external_thread_id`/`external_message_id` in `non_relational_ids`.

Backend suite fully green; frontend `npm run build`, `npm run lint`, `npm run test` all green.
