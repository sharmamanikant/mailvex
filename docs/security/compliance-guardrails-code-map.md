# Compliance Guardrails Code Map

This document maps the 24 product guardrails to implemented controls. The platform provides operational controls for permission-based email; it does not provide legal advice or guarantee compliance with a jurisdiction's law. Tenant configuration and human review remain part of the control model.

| # | Guardrail | Code / control |
|---:|---|---|
| 1 | Require current policy acceptance | `backend/app/services/compliance.py` policy gate; `backend/app/services/policies.py`; `GET /api/v1/policies/status`; `POST /api/v1/policies/documents/{policy_type}/accept` |
| 2 | Version policies and re-accept material changes | `CompliancePolicy` and `PolicyAcceptance` in `backend/app/models/entities.py`; `PolicyService.is_accepted()` compares the accepted and current versions |
| 3 | Record recipient consent metadata when configured | `Contact` consent/lawful-basis fields; `ComplianceProfile.require_consent_metadata`; campaign pre-flight in `backend/app/services/campaigns.py` |
| 4 | Keep tenant data isolated | Tenant-owned model constraints and tenant predicates throughout services and repositories; authentication creates a tenant-scoped `TenantPrincipal` |
| 5 | Provide unsubscribe support | `backend/app/services/sending.py` adds List-Unsubscribe headers; suppression and unsubscribe checks run in `backend/app/services/compliance.py` |
| 6 | Enforce suppression and opt-out checks | `Suppression` and `Unsubscribe` models plus recipient checks in `backend/app/services/compliance.py`; suppression API in `backend/app/api/suppression.py` |
| 7 | Validate recipients before sending | Contact validation status and campaign recipient pre-flight in `backend/app/services/campaigns.py`; send-time compliance evaluation in `backend/app/services/compliance.py` |
| 8 | Require an authorized sender identity | Sender connection/account ownership, authentication state, and sender checks in `backend/app/services/compliance_status.py` |
| 9 | Protect sender and domain authentication | Sender health and domain health services; `SENDER_AUTHENTICATION_REQUIRED` is a normalized blocking reason |
| 10 | Require human campaign approval | Campaign state transitions and immutable approval snapshot in `backend/app/services/campaigns.py`; delivery workers only process approved campaigns |
| 11 | Run server-side pre-flight checks | Campaign validation and send-time `ComplianceService.evaluate()` are server-side gates; client UI is never the authority |
| 12 | Pause risky senders automatically | `backend/app/services/safety.py` evaluates configured thresholds and sets `REVIEW_REQUIRED`, `paused_at`, and `resume_guard`; delivery events re-evaluate after telemetry |
| 13 | Require explicit human release after a pause | `SenderSafetyService.release()` and `POST /api/v1/compliance/senders/{sender_id}/release`; release is audited and never automatic |
| 14 | Respect provider policies and throttling | Provider adapters surface policy/rate failures; queue and delivery services defer retryable provider failures |
| 15 | Keep provider credentials out of application records | Provider credential references and encrypted credential handling in `backend/app/email_providers/credentials.py` and sender connection models |
| 16 | Verify OAuth state and redirect ownership | Google, Microsoft, and Zoho sender OAuth services bind short-lived state to tenant/user and validate callback state |
| 17 | Audit consequential compliance actions | `AuditService` records policy acceptance, profile updates, sender pauses/releases, and compliance outcomes |
| 18 | Configure jurisdiction-specific requirements | `ComplianceProfileService` stores jurisdiction and configurable gates; requirements are data/configuration, not hardcoded legal advice |
| 19 | Make retention configurable | `ComplianceProfile.retention_policy` and default retention settings in `backend/app/services/compliance_profile.py` |
| 20 | Prevent unsafe configuration values | `ComplianceProfileService.unsafe_threshold()` and the profile PATCH endpoint reject invalid safety ranges |
| 21 | Apply rate and usage limits | Billing usage enforcement and provider/delivery queue limits prevent unbounded sending; see `backend/app/billing/usage.py` and `backend/app/services/delivery_jobs.py` |
| 22 | Use minimum samples for safety telemetry | `safety_thresholds.min_sample_size` prevents rate decisions from tiny samples; rolling `window_days` bounds the evaluation window |
| 23 | Use normalized, machine-readable compliance states | `backend/app/services/compliance_status.py` defines `COMPLIANT`, `WARNING`, `BLOCKED`, `REVIEW_REQUIRED`, `UNKNOWN` and stable reason codes |
| 24 | Include unsubscribe controls in outbound messages | `backend/app/services/sending.py` generates one-click List-Unsubscribe headers when the configured profile requires them |

## Verification

Focused regression coverage lives in `backend/tests/test_policy_guardrails.py` and `backend/tests/test_compliance_guardrails.py`. The suites cover policy version acceptance, profile default merging and threshold sanity checks, protective pausing (including that a paused sender is never auto-resumed and only `release` re-enables it), stable reason normalization, sender/campaign compliance status, and the server-side campaign pre-flight gates. API behavior should be exercised with the existing authenticated API test fixtures when endpoint contracts change.
