"""Phase 5 Sender health orchestration.

``SenderHealthService`` runs the full health evaluation for one Sender:

  1. tenant-scoped authorization (sender must belong to the tenant)
  2. manual-check rate limiting (per sender, configurable interval)
  3. a compare-and-set lock on ``Sender.health_status = CHECKING`` so two
     concurrent runs can never double-evaluate; a stale lock (older than the
     configured TTL) is stolen so an interrupted run does not wedge a sender
  4. execute every check in the registry; a failing check is recorded as an
     UNKNOWN outcome - it never aborts the evaluation
  5. aggregate the score, derive the status, persist results + history, update
     the Sender's health fields, and emit audit events + structured logs

Triggered-by is MANUAL (API), SCHEDULED, or SYSTEM. No credentials, tokens,
or raw DNS payloads are written to the database or audit trail.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import func, select, update
from sqlalchemy.orm import Session, selectinload

from app.core.config import settings
from app.core.logging import log_event
from app.models import Sender, SenderHealthCheck, SenderHealthCheckResult
from app.security.rate_limit import RateLimitResult
from app.services.audit import AuditService
from app.services.sender_health_engine.checks import all_checks
from app.services.sender_health_engine.dns import SystemDnsResolver
from app.services.sender_health_engine.domain import extract_domain
from app.services.sender_health_engine.providers import build_provider_adapter
from app.services.sender_health_engine.scoring import (
    SCORE_VERSION,
    THRESHOLD_HEALTHY,
    THRESHOLD_WARNING,
    UNKNOWN_HANDLING,
    WEIGHTS,
    aggregate_score,
    overall_status,
    round_score,
)
from app.services.sender_health_engine.types import (
    CheckContext,
    CheckResult,
    HealthStatus,
    SenderHealthError,
)
from app.services.workspace_senders import SenderNotFoundError

logger = logging.getLogger("crcrm.sender_health")

SENDER_RESOURCE_TYPE = "sender"

SENDER_HEALTH_CHECK_STARTED = "SENDER_HEALTH_CHECK_STARTED"
SENDER_HEALTH_CHECK_COMPLETED = "SENDER_HEALTH_CHECK_COMPLETED"
SENDER_HEALTH_CHECK_FAILED = "SENDER_HEALTH_CHECK_FAILED"

CHECK_TYPE_ORDER = (
    "PROVIDER_CONNECTION",
    "MAILBOX_STATUS",
    "DOMAIN",
    "SPF",
    "DKIM",
    "DMARC",
    "DNS",
    "SENDING_CONFIGURATION",
    "SENDING_SIGNALS",
)


def _now() -> datetime:
    return datetime.now(UTC)


def _check_rank(check_type: str) -> int:
    return CHECK_TYPE_ORDER.index(check_type) if check_type in CHECK_TYPE_ORDER else len(CHECK_TYPE_ORDER)


class RateLimiter(Protocol):
    def check_limit(self, key: str, limit: int, window_seconds: int) -> RateLimitResult: ...


class SenderHealthService:
    def __init__(
        self,
        session: Session,
        tenant_id: UUID,
        actor_id: UUID | None = None,
        *,
        resolver: Any | None = None,
        rate_limiter: RateLimiter | None = None,
    ) -> None:
        self.session = session
        self.tenant_id = tenant_id
        self.actor_id = actor_id
        self.resolver = resolver if resolver is not None else SystemDnsResolver()
        self.rate_limiter = rate_limiter

    # ------------------------------------------------------------------ #
    # Read helpers
    # ------------------------------------------------------------------ #
    def get_sender(self, sender_id: UUID) -> Sender:
        sender = self.session.scalars(
            select(Sender)
            .where(Sender.id == sender_id, Sender.tenant_id == self.tenant_id)
            .options(selectinload(Sender.mailbox), selectinload(Sender.provider_connection))
        ).first()
        if sender is None:
            raise SenderNotFoundError()
        return sender

    def _audit(self, action: str, resource_id: UUID, metadata: dict[str, Any]) -> None:
        AuditService(self.session, self.tenant_id, self.actor_id).record(
            action, SENDER_RESOURCE_TYPE, resource_id, metadata
        )

    # ------------------------------------------------------------------ #
    # Run
    # ------------------------------------------------------------------ #
    def run_health_check(self, sender_id: UUID, *, triggered_by: str = "MANUAL") -> dict[str, Any]:
        if triggered_by not in {"MANUAL", "SCHEDULED", "SYSTEM"}:
            raise SenderHealthError("Unsupported triggered_by value", status_code=400)
        started = _now()
        sender = self.get_sender(sender_id)

        self._enforce_rate_limit(sender.id, triggered_by)
        self._acquire_lock(sender, started)

        health_check = SenderHealthCheck(
            tenant_id=self.tenant_id,
            sender_id=sender.id,
            overall_status="CHECKING",
            score_version=SCORE_VERSION,
            triggered_by=triggered_by,
            started_at=started,
        )
        self.session.add(health_check)
        self._audit(
            SENDER_HEALTH_CHECK_STARTED,
            sender.id,
            {"sender_id": str(sender.id), "email": sender.email, "triggered_by": triggered_by},
        )
        self.session.commit()
        log_event(
            "health_check_started",
            tenant_id=str(self.tenant_id),
            user_id=str(self.actor_id) if self.actor_id else None,
            sender_id=str(sender.id),
            provider=sender.provider,
            failure_code=triggered_by,
        )

        try:
            results, score, status = self._evaluate(sender, started)
            completed = _now()
            duration_ms = max(0, int((completed - health_check.started_at).total_seconds() * 1000))
            self._persist_results(health_check, results, score, status, sender, completed, duration_ms)
            self._audit(
                SENDER_HEALTH_CHECK_COMPLETED,
                sender.id,
                {
                    "sender_id": str(sender.id),
                    "health_check_id": str(health_check.id),
                    "overall_status": status,
                    "overall_score": float(score) if score is not None else None,
                    "score_version": SCORE_VERSION,
                    "triggered_by": triggered_by,
                    "duration_ms": health_check.duration_ms,
                },
            )
            self.session.commit()
            log_event(
                "health_check_completed",
                tenant_id=str(self.tenant_id),
                user_id=str(self.actor_id) if self.actor_id else None,
                sender_id=str(sender.id),
                provider=sender.provider,
                failure_code=status,
            )
        except SenderHealthError:
            self.session.rollback()
            raise
        except Exception as exc:
            self.session.rollback()
            self._record_failed_run(sender, started, "HEALTH_CHECK_RUN_FAILED", str(exc), triggered_by)
            raise SenderHealthError(
                "Sender health check failed to complete",
                status_code=500,
                code="HEALTH_CHECK_RUN_FAILED",
            ) from exc

        shown_score = Decimal(str(score)) if score is not None else None
        results_payload = [self._outcome_payload(outcome) for outcome in results]
        return self._check_payload(health_check, shown_score, status, results_payload)

    def _enforce_rate_limit(self, sender_id: UUID, triggered_by: str) -> None:
        if triggered_by != "MANUAL" or self.rate_limiter is None:
            return
        window = int(getattr(settings, "sender_health_manual_min_interval_seconds", 60))
        key = f"crcrm:sender-health:manual:{self.tenant_id}:{sender_id}"
        result = self.rate_limiter.check_limit(key, 1, window)
        if not result.allowed:
            raise SenderHealthError(
                "A manual health check was recently run for this sender. Try again shortly.",
                status_code=429,
                code="HEALTH_CHECK_RATE_LIMITED",
            )

    def _acquire_lock(self, sender: Sender, now: datetime) -> None:
        taken = self.session.execute(
            update(Sender)
            .where(
                Sender.id == sender.id,
                Sender.tenant_id == self.tenant_id,
                Sender.health_status != "CHECKING",
            )
            .values(health_status="CHECKING", last_health_check_at=now, updated_at=now)
        )
        if int(getattr(taken, "rowcount", 0)):
            return
        ttl = float(getattr(settings, "sender_health_lock_ttl_seconds", 300))
        last = sender.last_health_check_at
        stuck = last is not None and (now - last.replace(tzinfo=UTC)).total_seconds() >= ttl
        if not stuck:
            raise SenderHealthError(
                "A sender health check is already running for this sender.",
                status_code=409,
                code="HEALTH_CHECK_IN_PROGRESS",
            )
        self.session.execute(
            update(Sender)
            .where(Sender.id == sender.id, Sender.tenant_id == self.tenant_id)
            .values(health_status="CHECKING", last_health_check_at=now, updated_at=now)
        )

    def _evaluate(
        self, sender: Sender, started: datetime
    ) -> tuple[list[CheckResult], float | None, HealthStatus]:
        domain = extract_domain(sender.email)
        adapter = build_provider_adapter(sender.provider)
        ctx = CheckContext(
            sender=sender,
            mailbox=sender.mailbox,
            connection=sender.provider_connection,
            domain=domain,
            resolver=self.resolver,
            provider_adapter=adapter,
            now=started,
        )
        results: list[CheckResult] = []
        for check_type, fn in all_checks():
            try:
                outcome = fn(ctx)
            except Exception:
                logger.exception("Sender health check '%s' failed to execute", check_type)
                log_event(
                    "health_check_completed",
                    tenant_id=str(self.tenant_id),
                    user_id=str(self.actor_id) if self.actor_id else None,
                    sender_id=str(sender.id),
                    check_type=check_type,
                    provider=sender.provider,
                    failure_code="check-run-failed",
                )
                outcome = CheckResult(
                    check_type,
                    "UNKNOWN",
                    "Check failed to execute",
                    summary="The check raised an unexpected error and could not produce a result.",
                    severity="MEDIUM",
                    metadata={"error": "INTERNAL"},
                )
            results.append(outcome)
        results.sort(key=lambda r: _check_rank(r.check_type))
        score, _ = aggregate_score(results)
        status = overall_status(results, score)
        return results, score, status

    def _persist_results(
        self,
        health_check: SenderHealthCheck,
        results: list[CheckResult],
        score: float | None,
        status: HealthStatus,
        sender: Sender,
        completed: datetime,
        duration_ms: int,
    ) -> None:
        for outcome in results:
            self.session.add(
                SenderHealthCheckResult(
                    tenant_id=self.tenant_id,
                    health_check_id=health_check.id,
                    check_type=outcome.check_type,
                    status=outcome.status,
                    score=Decimal(str(outcome.score)) if outcome.score is not None else None,
                    severity=outcome.severity,
                    title=outcome.title,
                    summary=outcome.summary,
                    technical_details=outcome.technical_details,
                    recommendation=outcome.recommendation,
                    result_metadata=dict(outcome.metadata),
                    checked_at=completed,
                )
            )
        health_check.overall_status = status
        health_check.overall_score = round_score(score) if score is not None else None
        health_check.completed_at = completed
        health_check.duration_ms = duration_ms
        sender.health_status = status
        sender.health_score = round_score(score) if score is not None else None
        sender.last_health_check_at = completed
        sender.updated_at = completed

    def _record_failed_run(
        self,
        sender: Sender,
        started: datetime,
        error_code: str,
        error_message: str,
        triggered_by: str,
    ) -> None:
        try:
            health_check = SenderHealthCheck(
                tenant_id=self.tenant_id,
                sender_id=sender.id,
                overall_status="UNKNOWN",
                score_version=SCORE_VERSION,
                triggered_by=triggered_by,
                started_at=started,
                completed_at=_now(),
                error_code=error_code,
                error_message=error_message[:500],
            )
            self.session.add(health_check)
            sender.health_status = "UNKNOWN"
            sender.health_score = None
            sender.last_health_check_at = started
            sender.updated_at = started
            self._audit(
                SENDER_HEALTH_CHECK_FAILED,
                sender.id,
                {"sender_id": str(sender.id), "error_code": error_code, "triggered_by": triggered_by},
            )
            self.session.commit()
        except Exception:
            self.session.rollback()
        log_event(
            "health_check_failed",
            tenant_id=str(self.tenant_id),
            user_id=str(self.actor_id) if self.actor_id else None,
            sender_id=str(sender.id),
            provider=sender.provider,
            failure_code=error_code,
        )

    # ------------------------------------------------------------------ #
    # Read paths
    # ------------------------------------------------------------------ #
    def latest_health(self, sender_id: UUID) -> dict[str, Any] | None:
        check = self.session.scalars(
            select(SenderHealthCheck)
            .where(
                SenderHealthCheck.tenant_id == self.tenant_id,
                SenderHealthCheck.sender_id == sender_id,
            )
            .order_by(SenderHealthCheck.created_at.desc())
            .limit(1)
            .options(selectinload(SenderHealthCheck.results))
        ).first()
        if check is None:
            return None
        results = [
            self._result_payload(row)
            for row in sorted(check.results, key=lambda r: (_check_rank(r.check_type), str(r.id)))
        ]
        return self._check_payload(
            check,
            Decimal(str(check.overall_score)) if check.overall_score is not None else None,
            check.overall_status,
            results,
        )

    def list_history(self, sender_id: UUID, *, page: int = 1, page_size: int = 25) -> dict[str, Any]:
        page = max(page, 1)
        page_size = min(max(page_size, 1), 200)
        filters = [
            SenderHealthCheck.tenant_id == self.tenant_id,
            SenderHealthCheck.sender_id == sender_id,
        ]
        total = int(self.session.scalar(select(func.count(SenderHealthCheck.id)).where(*filters)) or 0)
        checks = list(
            self.session.scalars(
                select(SenderHealthCheck)
                .where(*filters)
                .order_by(SenderHealthCheck.created_at.desc())
                .offset((page - 1) * page_size)
                .limit(page_size)
            ).all()
        )
        counts: dict[UUID, int] = {}
        if checks:
            count_rows = self.session.execute(
                select(SenderHealthCheckResult.health_check_id, func.count(SenderHealthCheckResult.id))
                .where(
                    SenderHealthCheckResult.tenant_id == self.tenant_id,
                    SenderHealthCheckResult.health_check_id.in_([c.id for c in checks]),
                )
                .group_by(SenderHealthCheckResult.health_check_id)
            ).all()
            counts = {row[0]: int(row[1]) for row in count_rows}
        items = [self._check_summary(check, counts.get(check.id, 0)) for check in checks]
        total_pages = int((total + page_size - 1) / page_size) if page_size else 0
        return {
            "sender_id": sender_id,
            "items": items,
            "page": page,
            "page_size": page_size,
            "total": total,
            "total_pages": total_pages,
        }

    # ------------------------------------------------------------------ #
    # Payload builders (dict shapes the pydantic response models accept)
    # ------------------------------------------------------------------ #
    def _check_payload(
        self,
        check: SenderHealthCheck,
        shown_score: Decimal | None,
        shown_status: str,
        results: list[dict[str, Any]],
    ) -> dict[str, Any]:
        return {
            "health_check_id": check.id,
            "sender_id": check.sender_id,
            "tenant_id": check.tenant_id,
            "overall_status": shown_status,
            "overall_score": shown_score,
            "score_version": check.score_version or SCORE_VERSION,
            "triggered_by": check.triggered_by,
            "started_at": check.started_at,
            "completed_at": check.completed_at,
            "duration_ms": check.duration_ms,
            "error_code": check.error_code,
            "error_message": check.error_message,
            "results": results,
            "score_explanation": self._score_explanation(),
        }

    def _result_payload(self, row: SenderHealthCheckResult) -> dict[str, Any]:
        return {
            "id": row.id,
            "check_type": row.check_type,
            "status": row.status,
            "score": row.score,
            "severity": row.severity,
            "title": row.title,
            "summary": row.summary,
            "technical_details": row.technical_details,
            "recommendation": row.recommendation,
            "metadata": dict(row.result_metadata or {}),
            "checked_at": row.checked_at,
        }

    @staticmethod
    def _outcome_payload(outcome: CheckResult) -> dict[str, Any]:
        return {
            "id": None,
            "check_type": outcome.check_type,
            "status": outcome.status,
            "score": Decimal(str(outcome.score)) if outcome.score is not None else None,
            "severity": outcome.severity,
            "title": outcome.title,
            "summary": outcome.summary,
            "technical_details": outcome.technical_details,
            "recommendation": outcome.recommendation,
            "metadata": dict(outcome.metadata),
            "checked_at": _now(),
        }

    def _check_summary(self, check: SenderHealthCheck, result_count: int) -> dict[str, Any]:
        return {
            "health_check_id": check.id,
            "triggered_by": check.triggered_by,
            "overall_status": check.overall_status,
            "overall_score": check.overall_score,
            "score_version": check.score_version or SCORE_VERSION,
            "started_at": check.started_at,
            "completed_at": check.completed_at,
            "duration_ms": check.duration_ms,
            "error_code": check.error_code,
            "result_count": result_count,
        }

    @staticmethod
    def _score_explanation() -> dict[str, Any]:
        return {
            "version": SCORE_VERSION,
            "weights": {key: value for key, value in WEIGHTS.items()},
            "unknown_handling": UNKNOWN_HANDLING,
            "thresholds": {"healthy": THRESHOLD_HEALTHY, "warning": THRESHOLD_WARNING},
        }


def build_health_overview_payload(
    sender: Sender,
    latest: dict[str, Any] | None,
) -> dict[str, Any]:
    domain_results: list[dict[str, Any]] = []
    summary = None
    if latest is not None:
        domain_results = [item for item in latest["results"] if item["check_type"] in {"SPF", "DKIM", "DMARC", "DNS"}]
        status = latest["overall_status"]
        if status == "HEALTHY":
            summary = "Sender configuration health looks good across the checks performed."
        elif status == "WARNING":
            summary = "Sender configuration health needs attention; review the flagged checks."
        elif status == "CRITICAL":
            summary = "Sender configuration health is critical; resolve the failing checks before sending."
        else:
            summary = "Sender health could not be fully determined; re-run the check."
    return {
        "id": sender.id,
        "email": sender.email,
        "provider": sender.provider,
        "health_status": sender.health_status,
        "health_score": sender.health_score,
        "last_health_check_at": sender.last_health_check_at,
        "latest": latest,
        "summary": summary,
        "domain_authentication_summary": domain_results,
    }