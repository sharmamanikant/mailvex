"""Shared types for the Phase 5 Sender health engine.

The engine evaluates the sender configuration, its provider connection,
mailbox state, domain DNS records (SPF/DKIM/DMARC/MX), sending configuration,
and any available sending signals. It is strictly observed-signal based: it
never fabricates a score, never guesses DNS records, and never claims inbox
placement guarantees.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import TYPE_CHECKING, Any, Literal
from uuid import UUID

if TYPE_CHECKING:
    from app.models import Mailbox, ProviderConnection, Sender
    from app.services.sender_health_engine.dns import DnsResolver
    from app.services.sender_health_engine.providers import ProviderHealthAdapter

CheckStatus = Literal["PASS", "WARNING", "FAIL", "UNKNOWN", "NOT_APPLICABLE"]
Severity = Literal["INFO", "LOW", "MEDIUM", "HIGH", "CRITICAL"]
HealthStatus = Literal["UNKNOWN", "HEALTHY", "WARNING", "CRITICAL", "CHECKING"]
TriggerType = Literal["MANUAL", "SCHEDULED", "SYSTEM"]

#: Every check the engine knows about, in deterministic evaluation order.
CHECK_TYPES: tuple[str, ...] = (
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

#: Check types that are weighted in the overall score (see ``scoring.py``).
WEIGHTED_CHECK_TYPES: tuple[str, ...] = (
    "PROVIDER_CONNECTION",
    "MAILBOX_STATUS",
    "SPF",
    "DKIM",
    "DMARC",
    "DNS",
    "SENDING_CONFIGURATION",
    "SENDING_SIGNALS",
)

#: Per-check outcome that is safe to persist and to return to clients.
@dataclass(frozen=True)
class CheckResult:
    check_type: str
    status: CheckStatus
    title: str
    summary: str | None = None
    technical_details: str | None = None
    recommendation: str | None = None
    severity: Severity = "INFO"
    #: 0-100 for known checks; None for UNKNOWN / NOT_APPLICABLE.
    score: int | None = None
    #: Sanitized metadata only - never credentials, tokens, or secret payloads.
    metadata: dict[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class CheckContext:
    """Everything a check function may inspect (never the DB session).

    DNS queries are restricted to ``domain`` (derived from the Sender's own
    validated email) through the injected ``resolver``, so the engine cannot
    be steered onto arbitrary caller-supplied hostnames.
    """

    sender: Sender
    mailbox: Mailbox | None
    connection: ProviderConnection | None
    domain: str | None
    resolver: DnsResolver
    provider_adapter: ProviderHealthAdapter
    now: datetime


#: A single check, in a stable order.
@dataclass(frozen=True)
class CheckSpec:
    check_type: str
    run: Any  # Callable[[CheckContext], CheckResult]


class SenderHealthError(Exception):
    """Domain error carrying an HTTP status and a stable machine code."""

    def __init__(self, message: str, status_code: int = 400, code: str | None = None) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


def sender_query_key(sender_id: UUID) -> str:
    return f"sender:{sender_id}"