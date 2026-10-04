from __future__ import annotations

import json
import re
from uuid import UUID

from sqlalchemy.orm import Session

from app.services.suppression import SuppressionService

# Intent classification taxonomy (Phase 18). Separate from the legacy
# classification labels used by older draft flows.
INTENTS = (
    "INTERESTED",
    "NOT_INTERESTED",
    "REQUEST_FOR_INFORMATION",
    "REQUEST_FOR_MEETING",
    "PRICE_REQUEST",
    "UNSUBSCRIBE",
    "OUT_OF_OFFICE",
    "WRONG_PERSON",
    "UNKNOWN",
)

# Sub-phrases that look like attempts to make the model override its own
# guardrails. NEVER used to change behaviour — only surfaced as a warning so a
# human can review. Incoming email is always treated as untrusted content.
_INJECTION_PATTERNS = (
    r"\bignore (your|prior|all|the) (instructions?|rules?|prompts?|system)\b",
    r"\bdisregard (your|all|the) (instructions?|rules?|prompt)\b",
    r"\bforget (your|everything|all) (previous|prior) (instructions?|rules?)\b",
    r"\boverr?ide (your|the) (instructions?|rules?|filters?)\b",
    r"\bbypass (your|the) (rules?|filters?|safety|guardrails?)\b",
    r"\b(send|share|reveal|expose|give me) (my |the )?(credentials?|password|api ?key|secret|tokens?)\b",
    r"\bdo not ?(follow|obey) (the )?(rules?|system)\b",
    r"\bpretend (you are|to be) (an admin|the system)\b",
    r"\bact as ?(an admin|the system|a developer|the ai)\b",
)

_UNSUBSCRIBE_PATTERNS = (
    r"\bunsubscribe\b",
    r"\bopt[- ]?out\b",
    r"\bremove me\b",
    r"\bstop (emailing|sending|the emails|these emails)\b",
    r"\bno more emails?\b",
    r"\bplease take me off\b",
    r"\bdo not (contact|email) me\b",
)

_SUBJECT_INJECTION_DIRECTIVE = re.compile(
    r"\b(ignore|disregard|override|bypass)\b", re.IGNORECASE
)


def detect_prompt_injection(text: str) -> list[str]:
    """Return a list of security warnings if ``text`` looks like an
    instruction-override attempt. This never alters behaviour by itself; it
    only flags content for human review."""
    normalized = (text or "").lower()
    hits: list[str] = []
    for pattern in _INJECTION_PATTERNS:
        if re.search(pattern, normalized):
            hits.append(
                "The inbound message contains wording that attempts to override "
                "application rules; it has been treated as content only."
            )
            break  # one representative warning is enough
    if _SUBJECT_INJECTION_DIRECTIVE.search(text or ""):
        hits.append(
            "The inbound message appears to instruct the system to relax its rules."
        )
    return hits


def detect_unsubscribe(text: str) -> bool:
    normalized = (text or "").lower()
    return any(re.search(pattern, normalized) for pattern in _UNSUBSCRIBE_PATTERNS)


def intent_is_unsubscribe(intent: str | None) -> bool:
    return (intent or "").upper() == "UNSUBSCRIBE"


def is_suppressed(
    session: Session, tenant_id: UUID, email: str | None
) -> bool:
    """Whether the reply recipient is on the suppression list.

    AI never overrides suppression rules — this check is authoritative.
    """
    if not email:
        return False
    return SuppressionService(session, tenant_id).is_suppressed(email)


def encode_warnings(warnings: list[str]) -> str | None:
    return json.dumps(warnings) if warnings else None


def decode_warnings(raw: str | None) -> list[str]:
    if not raw:
        return []
    try:
        value = json.loads(raw)
        if isinstance(value, list):
            return [str(item) for item in value]
    except (ValueError, TypeError):
        pass
    return []
