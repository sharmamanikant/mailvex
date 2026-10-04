# Smoke Test Runbook (Phase 25)

This runbook verifies the end-to-end CR+CRM flows required for the production
release gate. It accompanies `backend/tests/smoke_test.py` and the release-gate
checklist in
[docs/deployment/production-release-gate.md](../deployment/production-release-gate.md).

## Automated smoke test

A runnable script drives every flow that can be executed against a deployment
without external provider credentials, and reports provider-bound flows as
`SKIP`:

```bash
cd backend
python tests/smoke_test.py --base http://localhost:8000
```

Exit code is `0` when no executable flow fails.

### What the script exercises

| # | Flow                  | Method      | Pass criteria                     | Notes                                   |
| - | --------------------- | ----------- | --------------------------------- | --------------------------------------- |
| 1 | Login                 | `POST /auth/login` | 200 + access token        |                                         |
| 2 | Contact creation      | `POST /contacts` | 201 + id                 | unique email per run                    |
| 3 | Template creation     | `POST /templates` | 201 + current_version_id | unique name per run                     |
| 4 | AI draft (mock)       | `POST /ai/generate-email` | 201            | `AI_PROVIDER=mock` in non-prod          |
| 5 | Sender endpoint       | `GET /senders` | 200                          | real connection requires OAuth (`SKIP`) |
| 6 | Campaign creation     | `POST /campaigns` | 201                 | requires a connected sender (`SKIP`)    |
| 7 | Compliance check      | `POST /campaigns/{id}/validate` + `GET /compliance/campaigns/{id}/summary` | 200 | `SKIP` without sender |
| 8 | Schedule              | `POST /campaigns/{id}/schedule` | 200/201       | `SKIP` without sender                   |
| 9 | Campaign analytics    | `GET /campaigns/{id}/analytics` | 200            | `SKIP` without sender                   |
| 10| Analytics dashboard   | `GET /analytics/dashboard` | 200                    |                                         |
| 11| Audit log             | `GET /admin/audit-logs` | 200                       |                                         |
| 12| Contact import        | `POST /contacts/imports` (multipart) + `POST /contacts/imports/preview` | 202/200 | upload+validate+preview |
| 13| Delivery event webhook| `POST /delivery-events/{provider}/{tenant_id}` (HMAC-signed) | 200 | bounce flow; needs `WEBHOOK_SECRETS` |
| 14| Unsubscribe/suppression| `GET /suppression-entries` | 200             |                                         |
| 15| Test send             | `POST /senders/{id}/test-email`            | n/a            | **manual** – requires configured sender |

## Manual / provider-bound flows

The following cannot be fully automated against a developer stack because they
require real external accounts. Perform them once in the stage/production
environment against a configured OAuth + SMTP sender:

- **Sender connection** – `GET /oauth-connect` then complete the provider
  authorize/callback round trip; confirm a `HEALTHY` sender appears in
  `GET /senders`.
- **Test send** – `POST /senders/{id}/test-email` and confirm receipt.
- **Real AI draft** – set `AI_PROVIDER` to a real provider + credentials and
  regenerate a draft; verify cost/usage accounting.
- **Compliance + schedule + delivery** – with a connected sender, create a
  campaign, run `validate`, `schedule`, then observe delivery via
  `GET /campaigns/{id}/analytics` and delivery events.

## Environment prerequisites

- `WEBHOOK_SECRETS` must be set for the delivery-event webhook flow
  (e.g. `google=<secret>,microsoft=<secret>`). The script signs with the same
  provider secret (`dev-smoke-secret` in development only).
- The authenticated username (`owner@example.com`) must exist in the target
  environment with `Admin` role.

## Results convention

`PASS` = flow verified against the running deployment.
`FAIL` = flow returned an unexpected status; the script reports the actual
body so operators can investigate.
`SKIP` = flow depends on external credentials not configured in the deployment.
Operator review required at release time.
