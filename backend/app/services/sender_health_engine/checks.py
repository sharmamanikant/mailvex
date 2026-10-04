"""The provider-independent sender health checks.

Each check inspects only observed configuration (Sender / Mailbox /
ProviderConnection records) or DNS records for the Sender's own domain, and
always returns a normalized :class:`CheckResult`. A check never raises: it
translates DNS outages to UNKNOWN and configuration problems to WARNING/FAIL.

Copy guidance: findings describe the sender's configuration health and
deliverability-related signals - the UI never promises inbox placement.
"""

from __future__ import annotations

import logging
from collections.abc import Callable

from app.core.logging import log_event
from app.services.sender_health_engine.domain import extract_domain
from app.services.sender_health_engine.types import (
    CHECK_TYPES,
    CheckContext,
    CheckResult,
)

logger = logging.getLogger("crcrm.sender_health")


def _log_dns_failure(ctx: CheckContext, check_type: str, host: str, error: str | None) -> None:
    log_event(
        "dns_check_failed",
        tenant_id=str(ctx.sender.tenant_id),
        sender_id=str(ctx.sender.id),
        check_type=check_type,
        domain=host,
        provider=ctx.sender.provider,
        failure_code=error or "UNKNOWN",
    )


# ------------------------------------------------------------------ #
# PROVIDER_CONNECTION
# ------------------------------------------------------------------ #
def provider_connection_check(ctx: CheckContext) -> CheckResult:
    connection = ctx.connection
    if connection is None:
        return CheckResult(
            "PROVIDER_CONNECTION",
            "FAIL",
            "No provider connection",
            summary="This sender has no provider connection, so it cannot authorize mail to send.",
            recommendation="Reconnect a provider workspace and re-create the sender from one of its mailboxes.",
            severity="CRITICAL",
            score=0,
            metadata={"status": None},
        )
    status = connection.status.upper()
    credential_configured = bool(connection.credential_reference)
    common = {
        "status": status,
        "credential_configured": credential_configured,
        "workspace_domain": connection.workspace_domain,
    }
    if status == "CONNECTED" and credential_configured:
        return CheckResult(
            "PROVIDER_CONNECTION",
            "PASS",
            "Provider connection is healthy",
            summary="The provider workspace is connected and authorized to send.",
            recommendation="No action required. Re-check after any credential rotation.",
            severity="INFO",
            score=100,
            metadata=common,
        )
    if status in {"CONNECTING"}:
        return CheckResult(
            "PROVIDER_CONNECTION",
            "WARNING",
            "Provider connection is still connecting",
            summary="The workspace is connecting, so sending is not available yet.",
            recommendation="Wait for the connection to complete, then re-run this check.",
            severity="MEDIUM",
            score=50,
            metadata=common,
        )
    if status == "CONNECTED":
        return CheckResult(
            "PROVIDER_CONNECTION",
            "WARNING",
            "Provider connection has no stored credential",
            summary="The workspace is connected, but no credential is stored yet, so sending cannot be authorized.",
            recommendation="Complete the provider authorization, then re-run this check.",
            severity="MEDIUM",
            score=50,
            metadata=common,
        )
    if status == "ERROR":
        return CheckResult(
            "PROVIDER_CONNECTION",
            "FAIL",
            "Provider connection is in an error state",
            summary="The workspace connection is reporting an error and cannot be used for sending.",
            technical_details="Provider connection status is ERROR.",
            recommendation="Inspect the connection's last sync status and re-connect if needed.",
            severity="HIGH",
            score=0,
            metadata=common,
        )
    if status == "DISCONNECTED":
        log_event(
            "provider_check_failed",
            tenant_id=str(ctx.sender.tenant_id),
            sender_id=str(ctx.sender.id),
            provider=ctx.sender.provider,
            failure_code="DISCONNECTED",
        )
        return CheckResult(
            "PROVIDER_CONNECTION",
            "FAIL",
            "Provider connection is disconnected",
            summary="The provider workspace is disconnected and sending is unavailable.",
            recommendation="Reconnect the workspace before using this sender.",
            severity="CRITICAL",
            score=0,
            metadata=common,
        )
    if status == "REVOKED":
        log_event(
            "provider_check_failed",
            tenant_id=str(ctx.sender.tenant_id),
            sender_id=str(ctx.sender.id),
            provider=ctx.sender.provider,
            failure_code="REVOKED",
        )
        return CheckResult(
            "PROVIDER_CONNECTION",
            "FAIL",
            "Provider connection was revoked",
            summary="The workspace authorization was revoked, so this sender cannot send.",
            recommendation="Re-authorize the provider workspace, then re-run this check.",
            severity="CRITICAL",
            score=0,
            metadata=common,
        )
    return CheckResult(
        "PROVIDER_CONNECTION",
        "UNKNOWN",
        "Provider connection state is unknown",
        summary="The provider connection status could not be interpreted.",
        recommendation="Review the connection and re-run this check.",
        severity="MEDIUM",
        metadata=common,
    )


# ------------------------------------------------------------------ #
# MAILBOX_STATUS
# ------------------------------------------------------------------ #
def mailbox_status_check(ctx: CheckContext) -> CheckResult:
    mailbox = ctx.mailbox
    if mailbox is None:
        return CheckResult(
            "MAILBOX_STATUS",
            "FAIL",
            "No mailbox record",
            summary="This sender has no underlying mailbox, so it has no sending identity available.",
            recommendation="Re-create the sender from an eligible workspace mailbox.",
            severity="CRITICAL",
            score=0,
            metadata={"mailbox_status": None},
        )
    status = mailbox.provider_status.upper() or "UNKNOWN"
    common = {
        "mailbox_status": status,
        "is_suspended": mailbox.is_suspended,
        "is_deleted": mailbox.is_deleted,
    }
    if mailbox.is_suspended or mailbox.provider_status == "SUSPENDED":
        return CheckResult(
            "MAILBOX_STATUS",
            "FAIL",
            "Mailbox is suspended",
            summary="The underlying mailbox is suspended, so this sender cannot send.",
            recommendation="Restore the mailbox in the provider workspace, then re-run this check.",
            severity="CRITICAL",
            score=0,
            metadata=common,
        )
    if mailbox.is_deleted or mailbox.provider_status == "DELETED":
        return CheckResult(
            "MAILBOX_STATUS",
            "FAIL",
            "Mailbox was deleted",
            summary="The underlying mailbox was removed from the workspace, so this sender cannot send.",
            recommendation="Re-create the sender from an active mailbox.",
            severity="CRITICAL",
            score=0,
            metadata=common,
        )
    if status == "ACTIVE":
        return CheckResult(
            "MAILBOX_STATUS",
            "PASS",
            "Mailbox is active",
            summary="The underlying mailbox is active and usable.",
            recommendation="No action required.",
            severity="INFO",
            score=100,
            metadata=common,
        )
    return CheckResult(
        "MAILBOX_STATUS",
        "WARNING",
        f"Mailbox status is {status}",
        summary="The underlying mailbox has a non-active status that may interrupt sending.",
        recommendation="Review the mailbox status in the provider workspace.",
        severity="MEDIUM",
        score=50,
        metadata=common,
    )


# ------------------------------------------------------------------ #
# DOMAIN
# ------------------------------------------------------------------ #
def domain_check(ctx: CheckContext) -> CheckResult:
    domain = ctx.domain
    if domain is None:
        return CheckResult(
            "DOMAIN",
            "FAIL",
            "Sender email has no valid domain",
            summary="A valid domain could not be extracted from this sender's email, so DNS checks cannot run.",
            technical_details="extract_domain() returned None; the email is empty, malformed, or has an unsupported internationalized domain.",
            recommendation="Fix the sender's email address, then re-run this check.",
            severity="HIGH",
            score=0,
            metadata={"email_domain": None},
        )
    return CheckResult(
        "DOMAIN",
        "PASS",
        "Sender domain is valid",
        summary="The sender's email resolves to the domain that all DNS checks run against.",
        recommendation="No action required.",
        severity="INFO",
        score=100,
        metadata={"email_domain": domain},
    )


# ------------------------------------------------------------------ #
# SPF
# ------------------------------------------------------------------ #
def spf_check(ctx: CheckContext) -> CheckResult:
    domain = ctx.domain
    if domain is None:
        return CheckResult(
            "SPF",
            "NOT_APPLICABLE",
            "SPF could not be checked",
            summary="There is no valid sending domain to query.",
            recommendation="Fix the sender email, then re-run this check.",
            severity="INFO",
        )
    lookup = ctx.resolver.resolve_txt(domain)
    if lookup.error:
        _log_dns_failure(ctx, "SPF", domain, lookup.error)
        return CheckResult(
            "SPF",
            "UNKNOWN",
            "SPF record could not be verified",
            f"DNS returned an error ({lookup.error}) while looking up TXT records for {domain}.",
            "Your DNS provider may be temporarily unavailable.",
            "Confirm DNS resolves and re-run this check.",
            severity="MEDIUM",
            metadata={"email_domain": domain, "error": lookup.error},
        )
    spf_records = [record for record in lookup.records if record.lower().startswith("v=spf1")]
    if not spf_records:
        spf_txt = [record for record in lookup.records if record.lower().startswith("v=")]
        note = "There is no SPF record" if not spf_txt else "There is no SPF record (only other TXT records exist)"
        return CheckResult(
            "SPF",
            "FAIL",
            "No SPF record published",
            summary=f"{note}, so mail servers cannot verify this sender is authorized for {domain}.",
            recommendation="Publish a single SPF TXT record for the domain authorizing your sending provider.",
            severity="HIGH",
            score=0,
            metadata={"email_domain": domain, "record_count": 0},
        )
    if len(spf_records) > 1:
        return CheckResult(
            "SPF",
            "WARNING",
            "Multiple SPF records published",
            summary=f"{len(spf_records)} SPF records were found for {domain}; receivers stop at the first match, so a broken record order can invalidate the policy.",
            recommendation="Consolidate to a single SPF record for the domain.",
            technical_details="Multiple v=spf1 TXT records are a significant configuration warning.",
            severity="MEDIUM",
            score=60,
            metadata={"email_domain": domain, "record_count": len(spf_records)},
        )
    return CheckResult(
        "SPF",
        "PASS",
        "SPF record present",
        f"A single SPF record authorizing sending is published for {domain}.",
        "No action required.",
        severity="INFO",
        score=100,
        metadata={"email_domain": domain, "record_count": 1},
    )


# ------------------------------------------------------------------ #
# DKIM
# ------------------------------------------------------------------ #
def dkim_check(ctx: CheckContext) -> CheckResult:
    domain = ctx.domain
    if domain is None:
        return CheckResult(
            "DKIM",
            "NOT_APPLICABLE",
            "DKIM could not be checked",
            summary="There is no valid sending domain to query.",
            recommendation="Fix the sender email, then re-run this check.",
            severity="INFO",
        )
    selector = ctx.provider_adapter.dkim_selector(ctx.mailbox, ctx.connection)
    if not selector:
        return CheckResult(
            "DKIM",
            "UNKNOWN",
            "DKIM selector is not configured",
            "The provider adapter does not expose the DKIM selector for this sender, so DKIM cannot be verified.",
            "Configure a provider health adapter that can supply the signing selector.",
            "Check the provider documentation for its DKIM selector and configure it, then re-run this check.",
            severity="INFO",
            metadata={"email_domain": domain, "selector_discovered": False},
        )
    selector_host = f"{selector}._domainkey.{domain}"
    lookup = ctx.resolver.resolve_txt(selector_host)
    if lookup.error:
        _log_dns_failure(ctx, "DKIM", selector_host, lookup.error)
        return CheckResult(
            "DKIM",
            "UNKNOWN",
            "DKIM key could not be verified",
            f"DNS returned an error ({lookup.error}) while looking up {selector_host}.",
            "Your DNS provider may be temporarily unavailable.",
            "Confirm DNS resolves and re-run this check.",
            severity="MEDIUM",
            metadata={"email_domain": domain, "selector": selector, "error": lookup.error},
        )
    dkim_keys = [record for record in lookup.records if record.startswith("v=DKIM1") or "p=" in record]
    if not dkim_keys:
        return CheckResult(
            "DKIM",
            "FAIL",
            f"DKIM key not found for selector {selector}",
            f"No DKIM public key was found at {selector_host}.",
            "Publish the DKIM key record for email signed with selector.",
            f"Publish the DKIM DNS record for selector '{selector}' at {selector_host}.",
            severity="HIGH",
            score=0,
            metadata={"email_domain": domain, "selector": selector},
        )
    return CheckResult(
        "DKIM",
        "PASS",
        "DKIM key present",
        f"A DKIM public key is published for the '{selector}' selector on {domain}.",
        "No action required.",
        severity="INFO",
        score=100,
        metadata={"email_domain": domain, "selector": selector},
    )


# ------------------------------------------------------------------ #
# DMARC
# ------------------------------------------------------------------ #
def dmarc_check(ctx: CheckContext) -> CheckResult:
    domain = ctx.domain
    if domain is None:
        return CheckResult(
            "DMARC",
            "NOT_APPLICABLE",
            "DMARC could not be checked",
            summary="There is no valid sending domain to query.",
            recommendation="Fix the sender email, then re-run this check.",
            severity="INFO",
        )
    dmarc_host = f"_dmarc.{domain}"
    lookup = ctx.resolver.resolve_txt(dmarc_host)
    if lookup.error:
        _log_dns_failure(ctx, "DMARC", dmarc_host, lookup.error)
        return CheckResult(
            "DMARC",
            "UNKNOWN",
            "DMARC record could not be verified",
            f"DNS returned an error ({lookup.error}) while looking up {dmarc_host}.",
            "Your DNS provider may be temporarily unavailable.",
            "Confirm DNS resolves and re-run this check.",
            severity="MEDIUM",
            metadata={"email_domain": domain, "error": lookup.error},
        )
    dmarc_records = [record for record in lookup.records if record.lower().startswith("v=dmarc1")]
    if not dmarc_records:
        return CheckResult(
            "DMARC",
            "FAIL",
            "No DMARC record published",
            summary="Domain-based policies for spoofing protection are not published for this sender's domain.",
            recommendation="Publish a DMARC TXT record (start with p=none, then tighten to quarantine/reject).",
            severity="HIGH",
            score=0,
            metadata={"email_domain": domain},
        )
    tags = _parse_dmarc(dmarc_records[0])
    safe = {
        key: tags.get(key.upper())
        for key in ("p", "sp", "pct", "rua", "ruf", "adkim", "aspf")
    }
    policy = (tags.get("P") or "").lower()
    if policy == "none":
        return CheckResult(
            "DMARC",
            "WARNING",
            "DMARC policy is not enforced (p=none)",
            "A DMARC record exists but its policy is p=none, so unauthenticated mail is still accepted silently.",
            "Review DMARC aggregate reports (rua) before tightening the policy.",
            "Tighten the policy to p=quarantine (or p=reject) once reports look clean.",
            severity="MEDIUM",
            score=60,
            metadata={"email_domain": domain, **safe},
        )
    if policy in {"quarantine", "reject"}:
        return CheckResult(
            "DMARC",
            "PASS",
            "DMARC policy is enforced",
            f"A DMARC policy of p={policy} is published for {domain}.",
            "No action required.",
            severity="INFO",
            score=100,
            metadata={"email_domain": domain, **safe},
        )
    return CheckResult(
        "DMARC",
        "WARNING",
        "DMARC policy tag is missing or malformed",
        "A DMARC record exists but no valid 'p' policy tag could be parsed.",
        "Most receivers treat a missing policy as p=none.",
        "Publish a DMARC record with an explicit p=quarantine or p=reject policy.",
        severity="MEDIUM",
        score=60,
        metadata={"email_domain": domain, **safe},
    )


def _parse_dmarc(record: str) -> dict[str, str]:
    """Parse a DMARC TXT record into its tag/value pairs (upper-cased keys)."""
    tags: dict[str, str] = {}
    for part in record.split(";"):
        if "=" not in part:
            continue
        key, _, value = part.partition("=")
        key = key.strip().upper()
        value = value.strip()
        if key and value:
            tags[key] = value
    return tags


# ------------------------------------------------------------------ #
# DNS (MX)
# ------------------------------------------------------------------ #
def dns_mx_check(ctx: CheckContext) -> CheckResult:
    domain = ctx.domain
    if domain is None:
        return CheckResult(
            "DNS",
            "NOT_APPLICABLE",
            "MX could not be checked",
            summary="There is no valid sending domain to query.",
            recommendation="Fix the sender email, then re-run this check.",
            severity="INFO",
        )
    lookup = ctx.resolver.resolve_mx(domain)
    if lookup.error:
        _log_dns_failure(ctx, "DNS", domain, lookup.error)
        return CheckResult(
            "DNS",
            "UNKNOWN",
            "MX lookup failed",
            f"DNS returned an error ({lookup.error}) while looking up MX records for {domain}.",
            "Your DNS provider may be temporarily unavailable.",
            "Confirm DNS resolves and re-run this check.",
            severity="MEDIUM",
            metadata={"email_domain": domain, "error": lookup.error},
        )
    if not lookup.records:
        return CheckResult(
            "DNS",
            "WARNING",
            "No MX records for the sending domain",
            f"The domain {domain} does not publish MX records. This is a supporting signal: it does not block sending, but may indicate an incomplete domain setup.",
            "Deliverability also depends on sender reputation and content.",
            "Publish MX records if this domain should receive mail; otherwise verify the domain is a dedicated sending domain.",
            severity="MEDIUM",
            score=50,
            metadata={"email_domain": domain, "mx_count": 0},
        )
    return CheckResult(
        "DNS",
        "PASS",
        "MX records present",
        f"The domain {domain} publishes {len(lookup.records)} MX record(s).",
        "No action required.",
        severity="INFO",
        score=100,
        metadata={"email_domain": domain, "mx_count": len(lookup.records)},
    )


# ------------------------------------------------------------------ #
# SENDING_CONFIGURATION
# ------------------------------------------------------------------ #
def sending_configuration_check(ctx: CheckContext) -> CheckResult:
    sender = ctx.sender
    status = sender.status.upper()
    enabled = sender.sending_enabled
    mailbox_status = ctx.mailbox.provider_status if ctx.mailbox is not None else None
    connection_status = ctx.connection.status if ctx.connection is not None else None
    reason = _availability_reason(status, enabled, mailbox_status, connection_status)
    common = {
        "sender_status": status,
        "sending_enabled": enabled,
        "mailbox_status": mailbox_status,
        "connection_status": connection_status,
        "reason": reason,
    }
    if reason is None:
        return CheckResult(
            "SENDING_CONFIGURATION",
            "PASS",
            "Sending is available",
            "The sender is active, sending is enabled, and the mailbox and provider connection are usable.",
            "No action required.",
            severity="INFO",
            score=100,
            metadata=common,
        )
    return CheckResult(
        "SENDING_CONFIGURATION",
        "WARNING",
        "Sending is not available",
        f"The sender is not accepting traffic: {reason}.",
        "This is an operational signal, not a deliverability failure.",
        "Enable sending, or restore the mailbox / provider connection, then re-run this check.",
        severity="LOW",
        score=50,
        metadata=common,
    )


def _availability_reason(
    sender_status: str,
    sending_enabled: bool,
    mailbox_status: str | None,
    connection_status: str | None,
) -> str | None:
    """Mirror the Sender availability evaluation for the config check.

    Kept here (instead of importing the private service helper) so the health
    engine stays decoupled from the Sender service module. Order matches
    ``workspace_senders._availability_reason``: lifecycle first, then sending
    flag, then mailbox, then provider connection.
    """
    if sender_status == "REMOVED":
        return "SENDER_REMOVED"
    if sender_status == "ERROR":
        return "SENDER_ERROR"
    if sender_status == "REVOKED":
        return "SENDER_REVOKED"
    if sender_status == "DISABLED":
        return "SENDER_DISABLED"
    if not sending_enabled:
        return "SENDER_DISABLED_SENDING"
    if mailbox_status not in (None, "ACTIVE"):
        return "MAILBOX_UNAVAILABLE"
    if connection_status != "CONNECTED":
        return "PROVIDER_DISCONNECTED"
    return None


# ------------------------------------------------------------------ #
# SENDING_SIGNALS
# ------------------------------------------------------------------ #
def sending_signals_check(ctx: CheckContext) -> CheckResult:
    return CheckResult(
        "SENDING_SIGNALS",
        "UNKNOWN",
        "Sending signals are not measured yet",
        "No sending-history engine exists in this phase, so signals such as bounces, complaints, and throttling are not available.",
        "Sending signals will be evaluated from real sending history when it exists.",
        "Not an issue yet - this check will become active once sending history is available.",
        severity="INFO",
        metadata={"available": False},
    )


# ------------------------------------------------------------------ #
# Registry (deterministic order)
# ------------------------------------------------------------------ #
_CHECK_FUNCTIONS: dict[str, Callable[[CheckContext], CheckResult]] = {
    "PROVIDER_CONNECTION": provider_connection_check,
    "MAILBOX_STATUS": mailbox_status_check,
    "DOMAIN": domain_check,
    "SPF": spf_check,
    "DKIM": dkim_check,
    "DMARC": dmarc_check,
    "DNS": dns_mx_check,
    "SENDING_CONFIGURATION": sending_configuration_check,
    "SENDING_SIGNALS": sending_signals_check,
}


def all_checks() -> list[tuple[str, Callable[[CheckContext], CheckResult]]]:
    return [(name, _CHECK_FUNCTIONS[name]) for name in CHECK_TYPES]


def run_check(check_type: str, ctx: CheckContext) -> CheckResult:
    fn = _CHECK_FUNCTIONS.get(check_type)
    if fn is None:
        return CheckResult(
            check_type,
            "UNKNOWN",
            "Check is not implemented",
            summary="This check type has no implementation yet.",
            severity="INFO",
        )
    return fn(ctx)


__all__ = ["all_checks", "extract_domain", "run_check"]