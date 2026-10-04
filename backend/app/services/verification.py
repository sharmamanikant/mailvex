"""Contact verification: orchestration, scoring, and verdict persistence.

Scoring contract
----------------
The score is a **technical confidence signal** (0-100). It is deliberately not a
claim about personhood, and nothing is auto-excluded on the score alone.

Two deliberate non-assumptions from the spec:

* Free providers (Gmail / Outlook / Hotmail / Yahoo / iCloud) earn **no**
  penalty and no special treatment. ``john@gmail.com`` is a legitimate,
  technically valid address. A free mailbox is a *type*, not a risk signal.
* Role accounts (``info@``, ``sales@``) are **not** invalid. They score neutral
  and are reported as ``role_account = true``.

``contacts.validation_status`` is intentionally left untouched. It is
campaign-exclusion semantics - ``services/campaigns.py`` and
``services/compliance.py`` both drop INVALID / DISPOSABLE / ROLE_ACCOUNT
recipients - so a scoring pass must never write it, or a transient DNS failure
would quietly remove genuine contacts from a send.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Contact
from app.services import duplicate_detection as dup
from app.services.email_validation import (
    TYPE_BUSINESS,
    DomainResolver,
    EmailSignals,
    EmailValidator,
    RuleBook,
)
from app.services.phone_validation import (
    STATUS_NOT_PROVIDED,
    PhoneSignals,
    validate_phone,
)
from app.services.phone_validation import (
    STATUS_VALID as PHONE_VALID,
)
from app.services.smtp_validation import (
    SMTP_ACCEPTED,
    SMTP_REJECTED,
    SmtpProber,
    SmtpResult,
)

VERIFICATION_VERIFIED = "VERIFIED"
VERIFICATION_LIKELY_VALID = "LIKELY_VALID"
VERIFICATION_NEEDS_REVIEW = "NEEDS_REVIEW"
VERIFICATION_RISKY = "RISKY"
VERIFICATION_INVALID = "INVALID"
VERIFICATION_DUPLICATE = "DUPLICATE"
VERIFICATION_UNKNOWN = "UNKNOWN"

RISK_LOW = "LOW"
RISK_MEDIUM = "MEDIUM"
RISK_HIGH = "HIGH"
RISK_UNKNOWN = "UNKNOWN"

COMPANY_PRESENT = "PRESENT"
COMPANY_MISSING = "MISSING"
COMPANY_DOMAIN_CONSISTENT = "DOMAIN_CONSISTENT"
COMPANY_DOMAIN_INCONSISTENT = "DOMAIN_INCONSISTENT"

# Positive signal weights.
SCORE_VALID_SYNTAX = 25
SCORE_DOMAIN_EXISTS = 15
SCORE_MX_VALID = 25
SCORE_NOT_DISPOSABLE = 10
SCORE_PHONE_VALID = 8
SCORE_COMPANY_CONSISTENT = 5
SCORE_NO_DUPLICATE = 5
# Deliberately capped: an SMTP accept is one weak technical signal, not proof.
SCORE_SMTP_ACCEPTED = 8
SCORE_SMTP_REJECTED = -12

# Negative signal weights.
SCORE_INVALID_SYNTAX = -60
SCORE_DOMAIN_MISSING = -45
SCORE_NO_MX = -25
SCORE_DISPOSABLE = -20
SCORE_PHONE_INVALID = -6
SCORE_DUPLICATE = -12
SCORE_POSSIBLE_DUPLICATE = -5


@dataclass
class ContactVerdict:
    """Everything the engine learned about one contact."""

    contact_id: UUID
    verification_status: str
    verification_score: int
    risk_level: str
    email: EmailSignals
    phone: PhoneSignals
    duplicate_status: str
    duplicate_score: int | None
    company_status: str
    smtp: SmtpResult | None = None
    reasons: list[str] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "verification_status": self.verification_status,
            "verification_score": self.verification_score,
            "risk_level": self.risk_level,
            "email": self.email.as_dict(),
            "phone": self.phone.as_dict(),
            "smtp": None
            if self.smtp is None
            else {
                "status": self.smtp.status,
                "code": self.smtp.code,
                "message": self.smtp.message,
                "host": self.smtp.host,
                "catch_all": self.smtp.catch_all,
            },
            "duplicate_status": self.duplicate_status,
            "duplicate_score": self.duplicate_score,
            "company_status": self.company_status,
            "reasons": list(self.reasons),
        }


def _company_status(contact: Contact, signals: EmailSignals) -> str:
    company = dup.normalize_company(contact.company)
    if not company:
        return COMPANY_MISSING
    if signals.email_type != TYPE_BUSINESS:
        return COMPANY_DOMAIN_CONSISTENT
    domain_label = signals.domain.split(".")[0]
    if domain_label and domain_label in company:
        return COMPANY_DOMAIN_CONSISTENT
    return COMPANY_DOMAIN_INCONSISTENT


def score_signals(
    signals: EmailSignals,
    phone: PhoneSignals,
    duplicate: dup.DuplicateVerdict,
    company_status: str,
    smtp: SmtpResult | None,
) -> int:
    score = 0
    if signals.syntax_ok:
        score += SCORE_VALID_SYNTAX
    else:
        score += SCORE_INVALID_SYNTAX
    if signals.domain_status == "EXISTS":
        score += SCORE_DOMAIN_EXISTS
    elif signals.domain_status == "NXDOMAIN":
        score += SCORE_DOMAIN_MISSING
    if signals.mx_status == "VALID":
        score += SCORE_MX_VALID
    elif signals.mx_status == "MISSING":
        score += SCORE_NO_MX
    if signals.disposable:
        score += SCORE_DISPOSABLE
    else:
        score += SCORE_NOT_DISPOSABLE
    # A free mailbox is a type, not a risk. It contributes exactly like a
    # business domain: only the underlying technical checks moved the score.
    if phone.status == PHONE_VALID:
        score += SCORE_PHONE_VALID
    elif phone.status not in (STATUS_NOT_PROVIDED, "UNKNOWN"):
        score += SCORE_PHONE_INVALID
    if company_status == COMPANY_DOMAIN_CONSISTENT:
        score += SCORE_COMPANY_CONSISTENT
    if duplicate.status == dup.STATUS_DUPLICATE:
        score += SCORE_DUPLICATE
    elif duplicate.status == dup.STATUS_POSSIBLE_DUPLICATE:
        score += SCORE_POSSIBLE_DUPLICATE
    else:
        score += SCORE_NO_DUPLICATE
    if smtp is not None:
        if smtp.status == SMTP_ACCEPTED:
            score += SCORE_SMTP_ACCEPTED
        elif smtp.status == SMTP_REJECTED:
            score += SCORE_SMTP_REJECTED
    return max(0, min(100, score))


def classify(
    score: int,
    signals: EmailSignals,
    duplicate: dup.DuplicateVerdict,
    smtp: SmtpResult | None = None,
) -> tuple[str, str]:
    """Map signals + score to (verification_status, risk_level).

    ``VERIFIED`` is reserved for addresses whose mailbox answered at the SMTP
    RCPT stage. Without a positive SMTP signal the strongest honest claim is
    ``LIKELY_VALID`` - the spec forbids implying identity without a real
    identity-verification mechanism.
    """
    if not signals.syntax_ok or signals.domain_status == "NXDOMAIN":
        return VERIFICATION_INVALID, RISK_HIGH
    if duplicate.status == dup.STATUS_DUPLICATE:
        return VERIFICATION_DUPLICATE, RISK_MEDIUM
    if signals.disposable:
        return VERIFICATION_RISKY, RISK_HIGH
    smtp_accepted = smtp is not None and smtp.status == SMTP_ACCEPTED
    if score >= settings.validation_score_verified_min:
        if smtp_accepted:
            return VERIFICATION_VERIFIED, RISK_LOW
        return VERIFICATION_LIKELY_VALID, RISK_LOW
    if score >= settings.validation_score_likely_valid_min:
        return VERIFICATION_LIKELY_VALID, RISK_LOW
    if score >= settings.validation_score_needs_review_min:
        return VERIFICATION_NEEDS_REVIEW, RISK_MEDIUM
    if score <= 0 or (signals.domain_status == "UNKNOWN" and signals.mx_status == "UNKNOWN"):
        return VERIFICATION_UNKNOWN, RISK_UNKNOWN
    return VERIFICATION_RISKY, RISK_HIGH


class ContactVerifier:
    """Runs the full engine for one contact and persists the verdict."""

    def __init__(
        self,
        session: Session,
        tenant_id: UUID,
        rules: RuleBook | None = None,
        resolver: DomainResolver | None = None,
        prober: SmtpProber | None = None,
    ) -> None:
        self._session = session
        self._tenant_id = tenant_id
        self._rules = rules if rules is not None else RuleBook(session)
        self._resolver = resolver if resolver is not None else DomainResolver()
        self._prober = prober if prober is not None else SmtpProber()
        self._detector = dup.DuplicateDetector(session, tenant_id)
        self._group_cache: dict[str, list[Contact]] | None = None

    def _candidates(self) -> dict[str, list[Contact]]:
        if self._group_cache is None:
            self._group_cache = {}
            for _label, members in self._detector.candidate_groups():
                for member in members:
                    self._group_cache.setdefault(str(member.id), []).extend(
                        other for other in members if other.id != member.id
                    )
        return self._group_cache

    def verify(self, contact: Contact, probe_smtp: bool = True) -> ContactVerdict:
        tenant_key = str(self._tenant_id)
        validator = EmailValidator(self._rules, self._resolver)
        signals = validator.validate(contact.email or "", tenant_key)
        phone = validate_phone(contact.phone, tenant_key)

        smtp_result: SmtpResult | None = None
        if probe_smtp:
            resolution = self._resolver.resolve(signals.domain) if signals.domain else None
            if resolution is not None:
                smtp_result = self._prober.probe(signals.normalized, signals, resolution)
                if smtp_result.catch_all is not None:
                    signals.catch_all = smtp_result.catch_all
                signals.smtp_status = smtp_result.status

        candidates = self._candidates().get(str(contact.id), [])
        duplicate = self._detector.verdict_for(contact, candidates)
        company_status = _company_status(contact, signals)
        score = score_signals(signals, phone, duplicate, company_status, smtp_result)
        status, risk = classify(score, signals, duplicate, smtp_result)

        reasons = list(signals.reasons) + list(phone.reasons) + list(duplicate.reasons)
        if smtp_result is not None and smtp_result.message:
            reasons.append(f"smtp: {smtp_result.message}")
        verdict = ContactVerdict(
            contact_id=contact.id,
            verification_status=status,
            verification_score=score,
            risk_level=risk,
            email=signals,
            phone=phone,
            duplicate_status=duplicate.status,
            duplicate_score=duplicate.score,
            company_status=company_status,
            smtp=smtp_result,
            reasons=reasons,
        )
        self._persist(contact, verdict)
        return verdict

    def _persist(self, contact: Contact, verdict: ContactVerdict) -> None:
        signals = verdict.email
        contact.email_status = signals.email_status
        contact.email_type = signals.email_type
        contact.email_provider = signals.provider
        contact.domain_status = signals.domain_status
        contact.mx_status = signals.mx_status
        contact.smtp_status = signals.smtp_status
        contact.disposable = signals.disposable
        contact.role_account = signals.role_account
        contact.catch_all = signals.catch_all
        contact.phone_status = verdict.phone.status
        contact.phone_type = verdict.phone.phone_type
        contact.company_status = verdict.company_status
        contact.duplicate_status = verdict.duplicate_status
        contact.duplicate_score = verdict.duplicate_score
        contact.verification_score = verdict.verification_score
        contact.risk_level = verdict.risk_level
        contact.verification_status = verdict.verification_status
        contact.last_verified_at = datetime.now(UTC)
        contact.verification_details = verdict.as_dict()

    def verify_many(self, contact_ids: list[UUID], probe_smtp: bool = True) -> list[ContactVerdict]:
        if not contact_ids:
            return []
        contacts = list(
            self._session.scalars(
                select(Contact).where(
                    Contact.tenant_id == self._tenant_id, Contact.id.in_(contact_ids)
                )
            ).all()
        )
        self._group_cache = None
        verdicts = [self.verify(contact, probe_smtp=probe_smtp) for contact in contacts]
        self._session.commit()
        return verdicts
