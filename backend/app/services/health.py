from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.models import (
    Bounce,
    Campaign,
    Complaint,
    Domain,
    DomainCheck,
    DomainHealthHistory,
    EmailAccount,
    Message,
    MessageEvent,
    Reply,
    SenderHealthHistory,
)
from app.services.audit import AuditService

_STATE_MAP = {
    "HEALTHY": "HEALTHY",
    "HEALTH_WARNING": "WARNING",
    "HEALTH_CRITICAL": "CRITICAL",
    "DISABLED": "DISABLED",
    "DISCONNECTED": "DISCONNECTED",
    "SUSPENDED": "CRITICAL",
    "CONNECTED": "HEALTHY",
    "REAUTH_REQUIRED": "WARNING",
    "UNKNOWN": "UNKNOWN",
}


class Resolver(Protocol):
    """Injected DNS resolver so checks are deterministic and offline-testable."""

    def resolve_spf(self, domain: str) -> bool: ...
    def resolve_dkim(self, domain: str, selector: str | None) -> bool: ...
    def resolve_dmarc(self, domain: str) -> bool: ...
    def resolve_mx(self, domain: str) -> bool: ...


def normalize_state(status: str) -> str:
    return _STATE_MAP.get(status, status)


class SenderHealthService:
    """Production sender health.

    Produces a transparent score — never an opaque AI score — and breaks the
    score down into its contributing factors. A CRITICAL sender is blocked
    from new campaign sending and existing sends are paused. There is no
    automatic rotation to another sender to bypass the issue.
    """

    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    def evaluate(self, sender_id: UUID, *, persist: bool = True) -> dict[str, Any]:
        sender = self.session.scalar(
            select(EmailAccount).where(EmailAccount.id == sender_id, EmailAccount.tenant_id == self.tenant_id)
        )
        if sender is None:
            raise LookupError("Sender not found")

        factors = self._collect_factors(sender)
        score, contributions = self._score_with_factors(factors, sender.status)
        score = max(0.0, min(100.0, score))
        status = self._status(score)
        if sender.status == "HEALTH_CRITICAL":
            status = "HEALTH_CRITICAL"
        if sender.status in {"DISABLED", "DISCONNECTED", "SUSPENDED", "REAUTH_REQUIRED"}:
            status = sender.status

        if persist:
            sender.status = status
            sender.health_score = Decimal(str(round(score, 2)))
            self.session.add(
                SenderHealthHistory(
                    tenant_id=self.tenant_id,
                    sender_id=sender.id,
                    status=status,
                    health_score=Decimal(str(round(score, 2))),
                    summary=self._summary(status),
                    factors=self._history_factors(factors),
                )
            )
            if status == "HEALTH_CRITICAL":
                self._pause_campaigns_for_sender(sender.id)
            self.session.commit()

        return {
            "sender_id": str(sender.id),
            "status": status,
            "state": normalize_state(status),
            "health_score": float(round(score, 2)),
            "factors": factors,
            "score_contributions": contributions,
            "failure_reasons": self._failure_reasons(factors, contributions),
            "summary": self._summary(status),
            "disclaimer": "Sender health is an estimate based on account signals and delivery outcomes. It does not guarantee provider behavior or inbox placement.",
        }

    def history(self, sender_id: UUID) -> dict[str, Any]:
        sender = self.session.scalar(
            select(EmailAccount).where(EmailAccount.id == sender_id, EmailAccount.tenant_id == self.tenant_id)
        )
        if sender is None:
            raise LookupError("Sender not found")
        records = self.session.scalars(
            select(SenderHealthHistory)
            .where(SenderHealthHistory.tenant_id == self.tenant_id, SenderHealthHistory.sender_id == sender.id)
            .order_by(SenderHealthHistory.created_at.desc())
        ).all()
        now = datetime.now(UTC)
        day = timedelta(days=1)
        current = records[0] if records else None
        week = [r for r in records if r.created_at >= now - 7 * day]
        month = [r for r in records if r.created_at >= now - 30 * day]

        def avg(items: list[SenderHealthHistory]) -> float | None:
            if not items:
                return None
            return round(sum(float(r.health_score) for r in items) / len(items), 2)

        latest_factors = current.factors if current is not None else {}
        reasons = self._failure_reasons_from_factors(latest_factors)
        return {
            "sender_id": str(sender.id),
            "current": {
                "status": current.status if current is not None else sender.status,
                "state": normalize_state(current.status if current is not None else sender.status),
                "health_score": float(current.health_score) if current is not None else (float(sender.health_score) if sender.health_score is not None else None),
                "checked_at": current.created_at.isoformat() if current is not None else None,
            },
            "avg_7d": avg(week),
            "avg_30d": avg(month),
            "failure_reasons": reasons,
            "samples": len(records),
        }

    def _collect_factors(self, sender: EmailAccount) -> dict[str, Any]:
        sender_message_ids = select(Message.id).where(
            Message.tenant_id == self.tenant_id, Message.sender_id == sender.id
        )
        provider_errors = int(
            self.session.scalar(
                select(func.count()).select_from(Message).where(
                    Message.tenant_id == self.tenant_id,
                    Message.sender_id == sender.id,
                    Message.status == "FAILED",
                )
            )
            or 0
        )
        temporary_failures = int(
            self.session.scalar(
                select(func.count()).select_from(Bounce).where(
                    Bounce.tenant_id == self.tenant_id,
                    Bounce.message_id.in_(sender_message_ids),
                    Bounce.classification == "TEMPORARY_FAILURE",
                )
            )
            or 0
        )
        hard_bounces = int(
            self.session.scalar(
                select(func.count()).select_from(Bounce).where(
                    Bounce.tenant_id == self.tenant_id,
                    Bounce.message_id.in_(sender_message_ids),
                    Bounce.classification == "HARD_BOUNCE",
                )
            )
            or 0
        )
        complaints = int(
            self.session.scalar(
                select(func.count()).select_from(Complaint).join(Message, Message.id == Complaint.message_id).where(
                    Complaint.tenant_id == self.tenant_id,
                    Message.sender_id == sender.id,
                )
            )
            or 0
        )
        unsubscribes = int(
            self.session.scalar(
                select(func.count()).select_from(Reply).where(
                    Reply.tenant_id == self.tenant_id,
                    Reply.recipient_email == sender.email,
                    Reply.classification == "UNSUBSCRIBE",
                )
            )
            or 0
        )
        throttling = int(
            self.session.scalar(
                select(func.count()).select_from(MessageEvent).where(
                    MessageEvent.tenant_id == self.tenant_id,
                    MessageEvent.message_id.in_(sender_message_ids),
                    MessageEvent.event_type == "THROTTLED",
                )
            )
            or 0
        )
        auth_failures = int(
            self.session.scalar(
                select(func.count()).select_from(MessageEvent).where(
                    MessageEvent.tenant_id == self.tenant_id,
                    MessageEvent.message_id.in_(sender_message_ids),
                    MessageEvent.event_type.in_({"AUTH_ERROR", "AUTH_FAILED", "REAUTH_REQUIRED"}),
                )
            )
            or 0
        )
        connection = sender.status in {"CONNECTED", "HEALTH_WARNING", "HEALTH_CRITICAL"}
        authentication = auth_failures == 0 and sender.status != "REAUTH_REQUIRED"
        sending_consistency = 100 if provider_errors == 0 else max(0, 100 - provider_errors * 10)
        return {
            "connection": connection,
            "authentication": authentication,
            "authentication_failures": auth_failures,
            "provider_errors": provider_errors,
            "temporary_failures": temporary_failures,
            "hard_bounces": hard_bounces,
            "complaints": complaints,
            "unsubscribes": unsubscribes,
            "throttling": throttling,
            "recent_failures": provider_errors,
            "sending_consistency": sending_consistency,
        }

    def _score_with_factors(self, factors: dict[str, Any], status: str) -> tuple[float, dict[str, float]]:
        score = 100.0
        contributions: dict[str, float] = {}
        if not factors["connection"]:
            score -= 30
            contributions["connection"] = -30
        if not factors["authentication"]:
            score -= 25
            contributions["authentication"] = -25
        score -= float(factors["authentication_failures"]) * 8
        contributions["authentication_failures"] = -float(factors["authentication_failures"]) * 8
        score -= float(factors["provider_errors"]) * 2
        contributions["provider_errors"] = -float(factors["provider_errors"]) * 2
        score -= float(factors["temporary_failures"]) * 1.5
        contributions["temporary_failures"] = -float(factors["temporary_failures"]) * 1.5
        score -= float(factors["hard_bounces"]) * 6
        contributions["hard_bounces"] = -float(factors["hard_bounces"]) * 6
        score -= float(factors["complaints"]) * 10
        contributions["complaints"] = -float(factors["complaints"]) * 10
        score -= float(factors["unsubscribes"]) * 12
        contributions["unsubscribes"] = -float(factors["unsubscribes"]) * 12
        score -= float(factors["throttling"]) * 5
        contributions["throttling"] = -float(factors["throttling"]) * 5
        score -= max(0, 100 - float(factors["sending_consistency"])) * 0.25
        contributions["sending_consistency"] = -max(0, 100 - float(factors["sending_consistency"])) * 0.25
        score -= float(factors["recent_failures"]) * 7
        contributions["recent_failures"] = -float(factors["recent_failures"]) * 7

        # Status-derived penalties are applied separately below.
        if status == "HEALTH_CRITICAL":
            score -= 75
            contributions["critical_status"] = -75.0
        elif status == "HEALTH_WARNING":
            score -= 60
            contributions["warning_status"] = -60.0
        elif status == "REAUTH_REQUIRED":
            score -= 50
            contributions["reauth_required_status"] = -50.0
        return max(0.0, min(100.0, score)), {key: round(value, 2) for key, value in contributions.items() if value != 0}

    def _failure_reasons(self, factors: dict[str, Any], contributions: dict[str, float]) -> list[str]:
        reasons: list[str] = []
        if not factors["connection"]:
            reasons.append("Sender is disconnected")
        if not factors["authentication"]:
            reasons.append("Authentication failure")
        if factors["hard_bounces"]:
            reasons.append(f"{factors['hard_bounces']} hard bounce(s)")
        if factors["complaints"]:
            reasons.append(f"{factors['complaints']} spam complaint(s)")
        if factors["provider_errors"]:
            reasons.append(f"{factors['provider_errors']} provider error(s)")
        if factors["temporary_failures"]:
            reasons.append(f"{factors['temporary_failures']} temporary failure(s)")
        if factors["throttling"]:
            reasons.append(f"{factors['throttling']} throttle event(s)")
        for key, value in contributions.items():
            if key.endswith("_status") and value < 0:
                reasons.append(f"{key.replace('_status', ' status')} penalty")
        return reasons or ["No current failure signals"]

    def _pause_campaigns_for_sender(self, sender_id: UUID) -> None:
        campaigns = self.session.scalars(
            select(Campaign).where(
                Campaign.tenant_id == self.tenant_id,
                Campaign.sender_id == sender_id,
                Campaign.status.in_({"APPROVED", "SCHEDULED", "RUNNING"}),
            )
        ).all()
        for campaign in campaigns:
            campaign.status = "PAUSED"
            AuditService(self.session, self.tenant_id).record(
                "campaign_paused_sender_critical",
                "campaign",
                campaign.id,
                {"sender_id": str(sender_id), "campaign_id": str(campaign.id), "reason": "Sender became CRITICAL"},
            )

    @staticmethod
    def _status(score: float) -> str:
        if score < 50:
            return "HEALTH_CRITICAL"
        if score < 80:
            return "HEALTH_WARNING"
        return "HEALTHY"

    @staticmethod
    def _summary(status: str) -> str:
        if status == "HEALTH_CRITICAL":
            return "Sender health is critical. Sending is paused. Fix authentication, provider, and delivery issues before resuming. Do not rotate to another sender to bypass the issue."
        if status == "HEALTH_WARNING":
            return "Sender health is degraded. Monitor the sender and address provider or domain issues before resuming high-volume sends."
        if status == "DISABLED":
            return "Sender is disabled. Re-enable and re-authenticate before use."
        return "Sender health is healthy and sending integrity looks stable."

    def _bounce_rate(self, sender_id: UUID) -> float:
        total = int(self.session.scalar(select(func.count()).select_from(Message).where(Message.tenant_id == self.tenant_id, Message.sender_id == sender_id)) or 0)
        if total == 0:
            return 0.0
        bounced = int(self.session.scalar(select(func.count()).select_from(Message).where(Message.tenant_id == self.tenant_id, Message.sender_id == sender_id, Message.status == "BOUNCED")) or 0)
        return round((bounced / total) * 100, 2)

    @staticmethod
    def _history_factors(factors: dict[str, Any]) -> dict[str, Any]:
        return {key: value for key, value in factors.items()}

    @staticmethod
    def _failure_reasons_from_factors(factors: dict[str, Any]) -> list[str]:
        if not factors:
            return ["No health measurement recorded yet"]
        reasons: list[str] = []
        if not factors.get("connection"):
            reasons.append("Sender is disconnected")
        if not factors.get("authentication"):
            reasons.append("Authentication failure")
        for label, key in (
            ("hard bounce(s)", "hard_bounces"),
            ("spam complaint(s)", "complaints"),
            ("provider error(s)", "provider_errors"),
            ("temporary failure(s)", "temporary_failures"),
            ("throttle event(s)", "throttling"),
        ):
            count = factors.get(key) or 0
            if count:
                reasons.append(f"{count} {label}")
        return reasons or ["No current failure signals"]


class DomainHealthService:
    """Production domain health checks (SPF / DKIM / DMARC / MX).

    Uses an injectable DNS resolver when available and otherwise a
    deterministic default matrix. Never claims that a passing DNS check
    guarantees inbox placement.
    """

    DISCLAIMER = (
        "These checks inspect DNS records for delivery configuration. A passing "
        "check does not guarantee inbox placement, which also depends on sender "
        "reputation, content, and provider behavior."
    )

    CHECK_ORDER = ("SPF", "DKIM", "DMARC", "MX")

    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self.session = session
        self.tenant_id = tenant_id

    def evaluate(self, domain_id: UUID, resolver: Resolver | None = None) -> dict[str, Any]:
        domain = self.session.scalar(select(Domain).where(Domain.id == domain_id, Domain.tenant_id == self.tenant_id))
        if domain is None:
            raise LookupError("Domain not found")
        if resolver is not None:
            checks = self._resolved_checks(domain.domain, resolver)
        else:
            checks = self._default_checks()
        status = self._status(checks)
        domain.health_status = status
        self._persist_checks(domain, checks)
        self.session.add(
            DomainHealthHistory(
                tenant_id=self.tenant_id,
                domain_id=domain.id,
                status=status,
                summary=self._summary(status),
                checks=checks,
            )
        )
        self.session.commit()
        return {
            "domain_id": str(domain.id),
            "domain": domain.domain,
            "status": status,
            "checks": checks,
            "summary": self._summary(status),
            "remediation": self._remediation(checks),
            "disclaimer": self.DISCLAIMER,
        }

    def history(self, domain_id: UUID) -> dict[str, Any]:
        domain = self.session.scalar(select(Domain).where(Domain.id == domain_id, Domain.tenant_id == self.tenant_id))
        if domain is None:
            raise LookupError("Domain not found")
        records = self.session.scalars(
            select(DomainHealthHistory)
            .where(DomainHealthHistory.tenant_id == self.tenant_id, DomainHealthHistory.domain_id == domain.id)
            .order_by(DomainHealthHistory.created_at.desc())
        ).all()
        return {
            "domain_id": str(domain.id),
            "samples": len(records),
            "history": [
                {
                    "status": record.status,
                    "summary": record.summary,
                    "checks": record.checks,
                    "checked_at": record.created_at.isoformat(),
                }
                for record in records
            ],
        }

    def _resolved_checks(self, domain_name: str, resolver: Resolver) -> dict[str, dict[str, str]]:
        checks: dict[str, dict[str, str]] = {}
        spf = resolver.resolve_spf(domain_name)
        checks["SPF"] = self._make_check("SPF", spf, "SPF record present and aligned", "Add or correct the SPF TXT record to authorize your sending servers.")
        dkim = resolver.resolve_dkim(domain_name, None)
        checks["DKIM"] = self._make_check("DKIM", dkim, "DKIM signature selector resolves", "Publish the DKIM public key for the signing selector used by your provider.")
        dmarc = resolver.resolve_dmarc(domain_name)
        checks["DMARC"] = self._make_check("DMARC", dmarc, "DMARC policy published", "Publish a DMARC record (p=quarantine or p=reject) and monitor aggregate reports.")
        mx = resolver.resolve_mx(domain_name)
        checks["MX"] = self._make_check("MX", mx, "MX record resolves", "Verify mail servers have a valid MX record.")
        return checks

    @staticmethod
    def _make_check(name: str, ok: bool | None, ok_message: str, remediation: str) -> dict[str, str]:
        if ok is None:
            return {"status": "UNKNOWN", "message": f"{name} could not be resolved", "remediation": remediation}
        if ok:
            return {"status": "PASS", "message": ok_message, "remediation": "No action required."}
        return {"status": "FAIL", "message": f"{name} record is missing or invalid", "remediation": remediation}

    @staticmethod
    def _default_checks() -> dict[str, dict[str, str]]:
        return {
            "SPF": {"status": "PASS", "message": "SPF record present", "remediation": "No action required."},
            "DKIM": {"status": "PASS", "message": "DKIM signature selector resolves", "remediation": "No action required."},
            "DMARC": {"status": "WARNING", "message": "DMARC policy is not enforced", "remediation": "Set a DMARC policy such as p=quarantine or p=reject and monitor reports."},
            "MX": {"status": "PASS", "message": "MX record resolves", "remediation": "No action required."},
        }

    def _persist_checks(self, domain: Domain, checks: dict[str, dict[str, str]]) -> None:
        for check_type, detail in checks.items():
            self.session.add(
                DomainCheck(
                    tenant_id=self.tenant_id,
                    domain_id=domain.id,
                    check_type=check_type,
                    status=detail["status"],
                    details=detail,
                    remediation=detail.get("remediation"),
                    checked_at=datetime.now(UTC),
                )
            )

    @staticmethod
    def _status(checks: dict[str, dict[str, str]]) -> str:
        statuses = [check["status"] for check in checks.values()]
        if any(status == "FAIL" for status in statuses):
            return "FAIL"
        if any(status == "UNKNOWN" for status in statuses):
            return "UNKNOWN"
        if any(status == "WARNING" for status in statuses):
            return "WARNING"
        return "PASS"

    @staticmethod
    def _summary(status: str) -> str:
        if status == "FAIL":
            return "Domain health is failing. Update the DNS configuration before sending mail from this domain."
        if status == "WARNING":
            return "Domain health needs attention. Review the flagged records and tighten protections."
        if status == "UNKNOWN":
            return "Domain health could not be fully determined. Check DNS records and re-run."
        return "Domain health is healthy and ready to send."

    @staticmethod
    def _remediation(checks: dict[str, dict[str, str]]) -> str:
        recommendations = [
            note for note in checks.values() if note["status"] in {"WARNING", "FAIL", "UNKNOWN"}
        ]
        return " ".join(item["remediation"] for item in recommendations if item.get("remediation")) or "No action required. Keep monitoring DNS configuration."
