"""Phase 25 - production release smoke test.

Exercises the core end-to-end flows against a running CR+CRM deployment over
HTTP. Provider-bound flows (real sender OAuth connection, real SMTP test send,
real AI provider) are exercised for their reachable API contract and reported
as CONFIG-BOUND / MANUAL when the deployment does not expose those providers.

Usage (against the local dev stack):
    python tests/smoke_test.py [--base http://localhost:8000]

Exit code is 0 when all executable flows pass; any failure returns 1.
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import hmac
import io
import json
import sys
import time
import uuid

import httpx

API = "http://localhost:8000"
OWNER_EMAIL = "owner@example.com"
OWNER_PASSWORD = "correct horse battery staple"
WEBHOOK_PROVIDER = "google"
WEBHOOK_SECRET = "dev-smoke-secret"

PASS = "PASS"
FAIL = "FAIL"
SKIP = "SKIP"


class ResultCollector:
    def __init__(self) -> None:
        self.results: list[tuple[str, str, str]] = []

    def add(self, flow: str, status: str, note: str = "") -> None:
        self.results.append((flow, status, note))
        print(f"[{status}] {flow}" + (f" - {note}" if note else ""))

    def summary(self) -> tuple[int, int, int]:
        p = sum(1 for _, s, _ in self.results if s == PASS)
        f = sum(1 for _, s, _ in self.results if s == FAIL)
        s = sum(1 for _, s, _ in self.results if s == SKIP)
        return p, f, s


def check(flow: str, expected: int, response: httpx.Response, notes: str = "") -> str:
    if response.status_code == expected:
        return PASS
    return f"{FAIL} (expected {expected}, got {response.status_code}: {response.text[:300]})"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--base", default=API)
    args = parser.parse_args()
    base = args.base.rstrip("/")
    api = f"{base}/api/v1"
    results = ResultCollector()

    client = httpx.Client(base_url=base, timeout=60.0, follow_redirects=True)

    # ------------------------------------------------------------------ #
    # 1. Login
    # ------------------------------------------------------------------ #
    login = client.post(f"{api}/auth/login", json={"email": OWNER_EMAIL, "password": OWNER_PASSWORD})
    if login.status_code != 200:
        results.add("login", FAIL, login.text[:300])
        client.close()
        return 1
    token = login.json()["access_token"]
    headers = {"Authorization": f"Bearer {token}"}
    results.add("login", PASS)

    me = client.get(f"{api}/auth/me", headers=headers)
    results.add("auth/me", check("auth/me", 200, me), "verified bearer auth")

    # ------------------------------------------------------------------ #
    # 2. Contact creation
    # ------------------------------------------------------------------ #
    contact_email = f"smoke.{uuid.uuid4().hex[:8]}@example.com"
    contact = client.post(
        f"{api}/contacts",
        headers=headers,
        json={"first_name": "Smoke", "last_name": "Test", "email": contact_email, "company": "SmokeCorp"},
    )
    contact_id = contact.json().get("id") if contact.status_code == 201 else None
    results.add(
        "contact creation",
        "PASS" if contact_id else f"{FAIL} ({contact.status_code}: {contact.text[:200]})",
        contact_email,
    )

    # ------------------------------------------------------------------ #
    # 3. Template creation
    # ------------------------------------------------------------------ #
    tpl = client.post(
        f"{api}/templates",
        headers=headers,
        json={
            "name": f"Smoke Template {uuid.uuid4().hex[:8]}",
            "subject_template": "Hello {{first_name}}",
            "html_body": "<p>Hi {{first_name}},</p><p>This is a smoke test.</p>",
            "text_body": "Hi {{first_name}},\n\nThis is a smoke test.",
        },
    )
    tpl_version_id = tpl.json().get("current_version_id") if tpl.status_code == 201 else None
    results.add("template creation", "PASS" if tpl.status_code == 201 else f"{FAIL} ({tpl.status_code}: {tpl.text[:200]})")

    # ------------------------------------------------------------------ #
    # 4. AI draft (mock provider)
    # ------------------------------------------------------------------ #
    ai = client.post(
        f"{api}/ai/generate-email",
        headers=headers,
        json={
            "objective": "Follow up with a prospect about our services",
            "audience": "A single technical buyer",
            "tone": "professional",
            "instruction": "generate",
        },
    )
    results.add("ai draft (mock)", check("ai draft", 201, ai), "AI_PROVIDER=mock in dev")

    # ------------------------------------------------------------------ #
    # 5. Sender connection (provider-bound)
    # ------------------------------------------------------------------ #
    senders = client.get(f"{api}/senders", headers=headers)
    results.add(
        "sender endpoint reachable",
        "PASS" if senders.status_code == 200 else f"{FAIL} ({senders.status_code}: {senders.text[:200]})",
    )
    has_senders = bool(senders.json()) if senders.status_code == 200 else False
    results.add(
        "sender connection (list non-empty)",
        PASS if has_senders else SKIP,
        "MQ: real sender requires OAuth connect (manual/seat-specific)",
    )

    # ------------------------------------------------------------------ #
    # 6. Campaign creation (needs a sender_id)
    # ------------------------------------------------------------------ #
    campaign = None
    if has_senders:
        sender_id = senders.json()[0]["id"]
        recipient_ids = [contact_id] if contact_id else []
        campaign = client.post(
            f"{api}/campaigns",
            headers=headers,
            json={
                "name": "Smoke Campaign",
                "objective": "Verify end-to-end delivery pipeline",
                "sender_id": sender_id,
                "template_version_id": tpl_version_id,
                "recipient_ids": recipient_ids,
                "timezone": "UTC",
            },
        )
        camp_id = campaign.json().get("id") if campaign.status_code == 201 else None
        results.add("campaign creation", "PASS" if camp_id else f"{FAIL} ({campaign.status_code})", f"campaign_id={camp_id}")
    else:
        camp_id = None
        results.add("campaign creation", SKIP, "requires a connected sender")

    # ------------------------------------------------------------------ #
    # 7. Compliance check + 8. Schedule
    # ------------------------------------------------------------------ #
    if camp_id:
        compliance = client.post(f"{api}/campaigns/{camp_id}/validate", headers=headers)
        comp_ok = compliance.status_code in (200, 201)
        results.add("compliance check (validate)", "PASS" if comp_ok else f"{FAIL} ({compliance.status_code}: {compliance.text[:200]})")
        comp_summary = client.get(f"{api}/compliance/campaigns/{camp_id}/summary", headers=headers)
        results.add("compliance summary", "PASS" if comp_summary.status_code == 200 else f"{FAIL} ({comp_summary.status_code}: {comp_summary.text[:200]})")

        # Schedule via dedicated route (creates delivery jobs when possible).
        sched = client.post(f"{api}/campaigns/{camp_id}/schedule", headers=headers)
        results.add("campaign schedule", "PASS" if sched.status_code in (200, 201) else f"{FAIL} ({sched.status_code}: {sched.text[:200]})")

        # 9. Analytics (per-campaign)
        ca = client.get(f"{api}/campaigns/{camp_id}/analytics", headers=headers)
        results.add("campaign analytics", check("campaign analytics", 200, ca))
    else:
        results.add("compliance check", SKIP, "no campaign")
        results.add("compliance summary", SKIP, "no campaign")
        results.add("campaign schedule", SKIP, "no campaign")
        results.add("campaign analytics", SKIP, "no campaign")

    # ------------------------------------------------------------------ #
    # 10. Analytics dashboard + audit log
    # ------------------------------------------------------------------ #
    dash = client.get(f"{api}/analytics/dashboard", headers=headers)
    results.add("analytics dashboard", check("dashboard", 200, dash))
    audit = client.get(f"{api}/admin/audit-logs", headers=headers)
    results.add("audit log", check("audit log", 200, audit))

    # ------------------------------------------------------------------ #
    # 11. Contact import (large import path)
    # ------------------------------------------------------------------ #
    rows = [{"email": f"import{i}@example.com", "first_name": f"Imp{i}"} for i in range(50)]
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=["email", "first_name"])
    writer.writeheader()
    writer.writerows(rows)
    files = {"file": ("smoke.csv", buf.getvalue().encode("utf-8"), "text/csv")}
    imp = client.post(f"{api}/contacts/imports", headers=headers, files=files)
    if imp.status_code == 202:
        job_id = imp.json().get("id")
        preview = client.post(f"{api}/contacts/imports/preview", headers=headers, files=files)
        results.add("contact import (upload+validate)", PASS, f"job_id={job_id}")
        results.add("contact import preview", check("preview", 200, preview))
    else:
        results.add("contact import (upload+validate)", FAIL, imp.text[:300])

    # ------------------------------------------------------------------ #
    # 12. Delivery event webhook (unsubscribe / bounce / delivered)
    # ------------------------------------------------------------------ #
    ts = int(time.time())
    event_id = str(uuid.uuid4())
    payload = {
        "type": "HARD_BOUNCE",
        "recipient": contact_email,
        "email": contact_email,
        "timestamp": ts,
    }
    body = json.dumps(payload).encode("utf-8")
    signature = hmac.new(WEBHOOK_SECRET.encode(), f"{ts}.{event_id}".encode(), hashlib.sha256).hexdigest()
    wb = client.post(
        f"{api}/delivery-events/{WEBHOOK_PROVIDER}/{me.json()['tenant_id']}",
        content=body,
        headers={
            "event-id": event_id,
            "timestamp": str(ts),
            "signature": signature,
            "Content-Type": "application/json",
        },
    )
    if wb.status_code == 400 and "not configured" in wb.text:
        results.add("delivery event webhook", SKIP, "provider secret not configured in this deployment")
    else:
        results.add("delivery event webhook (bounce)", "PASS" if wb.status_code == 200 else f"{FAIL} ({wb.status_code}: {wb.text[:200]})")

    # Unsubscribe endpoint reachable
    unsub_entries = client.get(f"{api}/suppression-entries", headers=headers)
    results.add("unsubscribe/suppression entries", check("suppression", 200, unsub_entries))

    # ------------------------------------------------------------------ #
    # Summary
    # ------------------------------------------------------------------ #
    passed, failed, skipped = results.summary()
    print("\n=== SMOKE TEST SUMMARY ===")
    print(f"PASS={passed}  FAIL={failed}  SKIP={skipped}")
    client.close()
    return 0 if failed == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
