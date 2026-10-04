# Compliance-Gated Sending

The production send path is:

`Campaign Recipient -> ComplianceService -> Scheduler -> Sending Worker -> SendingService -> EmailProviderInterface -> MessageEvent`.

SendingService validates the scheduled message, campaign, sender, and recipient in the active tenant, runs all compliance checks, renders the approved template, sends through the provider adapter, stores the provider message ID and event, and updates campaign statistics. A blocked result is persisted as failed work with an immutable audit record and never reaches a provider.

Provider throttling remains deferred work. The scheduler owns retry timing and backoff; no quota bypass, sender rotation, or anti-detection behavior is permitted. Idempotency is enforced by the scheduled-message key and unique message constraint.