"""Deterministic, documented, versioned scoring for sender health.

The overall score is a weighted average over the checks that produced a KNOWN
outcome (PASS/WARNING/FAIL). UNKNOWN and NOT_APPLICABLE checks are excluded
and their weight is redistributed proportionally among the known checks - an
unknown check is never treated as a zero. The weights and the unknown-handling
rule are versioned in ``SCORE_VERSION`` and surfaced to the UI verbatim so a
score is never shown without its explanation.

Status thresholds (version ``v1``):
    * >= 80  -> HEALTHY
    * 60-79  -> WARNING
    * < 60   -> CRITICAL

Deterministic overrides:
    * any FAIL with severity CRITICAL forces CRITICAL
    * any other FAIL forces at most WARNING (a failed check always prevents
      a HEALTHY overall verdict)
    * if no known check exists the overall verdict is UNKNOWN (score None)
"""

from __future__ import annotations

from decimal import Decimal

from app.services.sender_health_engine.types import (
    CheckResult,
    HealthStatus,
)

SCORE_VERSION = "v1"

#: Percent weight per weighted check type. Sums to 100. DOMAIN intentionally
#: carries no separate weight: it gates the DNS checks but is not itself a
#: signal about the sender's sending setup.
WEIGHTS: dict[str, float] = {
    "PROVIDER_CONNECTION": 20.0,
    "MAILBOX_STATUS": 15.0,
    "SPF": 15.0,
    "DKIM": 15.0,
    "DMARC": 20.0,
    "DNS": 5.0,
    "SENDING_CONFIGURATION": 5.0,
    "SENDING_SIGNALS": 5.0,
}

THRESHOLD_HEALTHY = 80.0
THRESHOLD_WARNING = 60.0

UNKNOWN_HANDLING = (
    "Unknown or not-applicable checks are excluded and their weight is "
    "distributed proportionally among the checks that produced a result."
)


def check_weight(check_type: str) -> float:
    return WEIGHTS.get(check_type, 0.0)


def weighted_contribution(result: CheckResult) -> float | None:
    """Return ``score * weight / 100`` for a known check, else None."""
    if result.score is None:
        return None
    if result.status not in {"PASS", "WARNING", "FAIL"}:
        return None
    weight = check_weight(result.check_type)
    if weight <= 0:
        return None
    return (float(result.score) / 100.0) * weight


def aggregate_score(results: list[CheckResult]) -> tuple[float | None, dict[str, float]]:
    """Return ``(overall_score, contributions)`` over known checks.

    ``contributions`` maps each weighted check type to its normalized percent
    contribution (0..100) once weight redistribution is applied.
    """
    known_weight = 0.0
    raw_sum = 0.0
    raw_contributions: dict[str, float] = {}
    for result in results:
        contribution = weighted_contribution(result)
        if contribution is None:
            continue
        weight = check_weight(result.check_type)
        raw_sum += contribution
        known_weight += weight
        raw_contributions[result.check_type] = contribution
    if known_weight <= 0:
        return None, {}
    scale = 100.0 / known_weight
    overall = round(max(0.0, min(100.0, raw_sum * scale)), 2)
    contributions = {key: round(value * scale, 2) for key, value in raw_contributions.items()}
    return overall, contributions


def overall_status(results: list[CheckResult], score: float | None) -> HealthStatus:
    """Derive the deterministic overall verdict (version v1)."""
    if score is None:
        return "UNKNOWN"

    any_fail = [r for r in results if r.status == "FAIL"]
    if any(r.severity == "CRITICAL" for r in any_fail):
        return "CRITICAL"
    if score < THRESHOLD_WARNING:
        return "CRITICAL"
    if any_fail:
        return "WARNING"
    if score < THRESHOLD_HEALTHY:
        return "WARNING"
    return "HEALTHY"


def status_from_score(score: float) -> HealthStatus:
    if score < THRESHOLD_WARNING:
        return "CRITICAL"
    if score < THRESHOLD_HEALTHY:
        return "WARNING"
    return "HEALTHY"


def round_score(score: float) -> Decimal:
    return Decimal(str(round(score, 2)))


__all__ = [
    "SCORE_VERSION",
    "THRESHOLD_HEALTHY",
    "THRESHOLD_WARNING",
    "UNKNOWN_HANDLING",
    "WEIGHTS",
    "aggregate_score",
    "check_weight",
    "overall_status",
    "round_score",
    "status_from_score",
]