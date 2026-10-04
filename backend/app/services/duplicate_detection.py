"""Duplicate detection and duplicate genuineness scoring.

The pre-existing ``ContactService.duplicate_groups`` loads **every** contact in
the tenant into memory and groups in Python, which does not survive a
million-row tenant. This module resolves candidate groups with indexed SQL
``GROUP BY`` and only materialises the contacts inside a candidate group, so
memory stays proportional to the size of the match set rather than the tenant.

Note on exact emails: ``uq_contacts_tenant_email`` already forbids two contacts
in one tenant sharing an email, so a duplicate can never be an exact email
match. Real duplicates in this schema are the same person recorded twice with
different addresses, or the same company/team entered inconsistently - which is
exactly what the agreement scoring below is built to judge.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any
from uuid import UUID

from sqlalchemy import func, select
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import Contact

STATUS_UNIQUE = "UNIQUE"
STATUS_DUPLICATE = "DUPLICATE"
STATUS_POSSIBLE_DUPLICATE = "POSSIBLE_DUPLICATE"

_COMPANY_SUFFIXES = (
    " ltd",
    " limited",
    " pvt",
    " pvt ltd",
    " private",
    " inc",
    " inc.",
    " llp",
    " l.l.p.",
    " co",
    " co.",
    " company",
    " corp",
    " corporation",
    " plc",
    " gmbh",
    " bv",
    " ag",
)
_PUNCTUATION = re.compile(r"[^\w\s]")
_WHITESPACE = re.compile(r"\s+")

DEFAULT_GROUP_LIMIT = 500


def normalize_name(value: str | None) -> str:
    if not value:
        return ""
    cleaned = _PUNCTUATION.sub(" ", value.lower())
    return _WHITESPACE.sub(" ", cleaned).strip()


def normalize_company(value: str | None) -> str:
    if not value:
        return ""
    cleaned = _PUNCTUATION.sub(" ", value.lower())
    cleaned = _WHITESPACE.sub(" ", cleaned).strip()
    changed = True
    while changed:
        changed = False
        for suffix in _COMPANY_SUFFIXES:
            if cleaned.endswith(suffix):
                cleaned = cleaned[: -len(suffix)].strip()
                changed = True
    return cleaned


def normalize_phone(value: str | None) -> str:
    if not value:
        return ""
    digits = re.sub(r"\D", "", value)
    if not digits:
        return ""
    # Compare on the national significant number so +91 9325... and 9325...
    # resolve to the same key.
    return digits[-10:] if len(digits) > 10 else digits


def normalize_domain(email: str | None) -> str:
    if not email or "@" not in email:
        return ""
    return email.strip().lower().rpartition("@")[2]


@dataclass
class DuplicateVerdict:
    status: str
    score: int | None
    reasons: list[str]
    matched_with: list[UUID] = field(default_factory=list)

    def as_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "score": self.score,
            "reasons": list(self.reasons),
            "matched_with": [str(item) for item in self.matched_with],
        }


def _agreement_score(subject: Contact, other: Contact) -> tuple[int, list[str]]:
    """Weighted agreement between two contact records (0-100)."""
    score = 0
    reasons: list[str] = []

    subject_phone = normalize_phone(subject.phone)
    other_phone = normalize_phone(other.phone)
    if subject_phone and other_phone:
        if subject_phone == other_phone:
            score += 45
            reasons.append("identical normalized phone")
        elif subject_phone[-7:] == other_phone[-7:]:
            score += 20
            reasons.append("phone numbers share their last 7 digits")
        else:
            score -= 25
            reasons.append("different phone numbers")

    subject_name = normalize_name(f"{subject.first_name or ''} {subject.last_name or ''}")
    other_name = normalize_name(f"{other.first_name or ''} {other.last_name or ''}")
    if subject_name and other_name:
        if subject_name == other_name:
            score += 35
            reasons.append("identical normalized name")
        elif subject_name.split() == other_name.split():
            score += 30
            reasons.append("name tokens match in a different order")
        elif set(subject_name.split()) & set(other_name.split()):
            score += 12
            reasons.append("names share a token")
        else:
            score -= 20
            reasons.append("different names")

    subject_company = normalize_company(subject.company)
    other_company = normalize_company(other.company)
    if subject_company and other_company:
        if subject_company == other_company:
            score += 20
            reasons.append("identical normalized company")
        else:
            score -= 10
            reasons.append("different companies")

    subject_domain = normalize_domain(subject.email)
    other_domain = normalize_domain(other.email)
    if subject_domain and other_domain:
        if subject_domain == other_domain:
            score += 12
            reasons.append("same email domain")
        else:
            score -= 5
            reasons.append("different email domains")

    return max(0, min(100, score)), reasons


class DuplicateDetector:
    """Resolves candidate duplicate groups with SQL, then scores genuineness."""

    def __init__(self, session: Session, tenant_id: UUID) -> None:
        self._session = session
        self._tenant_id = tenant_id

    def _dialect(self) -> str:
        return self._session.get_bind().dialect.name

    def _phone_key_expression(self) -> Any | None:
        """Portable national-phone key, or ``None`` when the dialect lacks it.

        Postgres gets the indexed ``regexp_replace``/``right`` form. SQLite (used
        by the test suite) has no regex replace, so the caller falls back to a
        Python projection over the phone column only.
        """
        if self._dialect() == "postgresql":
            return func.right(func.regexp_replace(Contact.phone, r"\D", "", "g"), 10)
        return None

    def candidate_groups(self, limit: int = DEFAULT_GROUP_LIMIT) -> list[tuple[str, list[Contact]]]:
        groups: list[tuple[str, list[Contact]]] = []
        groups.extend(self._groups_by_phone(limit))
        groups.extend(self._groups_by_name_company(limit))
        return groups[:limit]

    def _phone_keys(self, limit: int) -> list[str]:
        expression = self._phone_key_expression()
        if expression is not None:
            rows = self._session.execute(
                select(expression.label("key"), func.count(Contact.id))
                .where(
                    Contact.tenant_id == self._tenant_id,
                    Contact.phone.is_not(None),
                    Contact.phone != "",
                )
                .group_by(expression)
                .having(func.count(Contact.id) > 1)
                .limit(limit)
            ).all()
            return [row.key for row in rows]
        phones = self._session.scalars(
            select(Contact.phone).where(
                Contact.tenant_id == self._tenant_id,
                Contact.phone.is_not(None),
                Contact.phone != "",
            )
        ).all()
        counts: dict[str, int] = {}
        for phone in phones:
            key = normalize_phone(phone)
            if key:
                counts[key] = counts.get(key, 0) + 1
        repeated = sorted(key for key, count in counts.items() if count > 1)
        return repeated[:limit]

    def _groups_by_phone(self, limit: int) -> list[tuple[str, list[Contact]]]:
        return [
            self._materialise(f"phone:{key}", "phone", key) for key in self._phone_keys(limit)
        ]

    def _groups_by_name_company(self, limit: int) -> list[tuple[str, list[Contact]]]:
        name = func.lower(func.trim(func.concat(Contact.first_name, " ", Contact.last_name)))
        company = func.lower(func.trim(Contact.company))
        rows = self._session.execute(
            select(name.label("key"), company.label("company"), func.count(Contact.id))
            .where(
                Contact.tenant_id == self._tenant_id,
                Contact.first_name.is_not(None),
                Contact.last_name.is_not(None),
                Contact.company.is_not(None),
                Contact.company != "",
            )
            .group_by(name, company)
            .having(func.count(Contact.id) > 1)
            .limit(limit)
        ).all()
        results: list[tuple[str, list[Contact]]] = []
        for row in rows:
            key = f"name_company:{row.key}|{row.company}"
            results.append(
                self._materialise(key, "name_company", row.key, company=row.company)
            )
        return results

    def _materialise(
        self,
        label: str,
        strategy: str,
        value: str,
        company: str | None = None,
    ) -> tuple[str, list[Contact]]:
        condition = None
        if strategy == "phone":
            expression = self._phone_key_expression()
            if expression is not None:
                condition = expression == value
        else:
            name = func.lower(func.trim(func.concat(Contact.first_name, " ", Contact.last_name)))
            condition = name == value
            if company is not None:
                condition = condition & (func.lower(func.trim(Contact.company)) == company)
        query = select(Contact).where(Contact.tenant_id == self._tenant_id)
        if condition is not None:
            query = query.where(condition)
        contacts = list(self._session.scalars(query).all())
        if strategy == "phone" and condition is None:
            contacts = [item for item in contacts if normalize_phone(item.phone) == value]
        return label, contacts

    def verdict_for(self, contact: Contact, candidates: list[Contact]) -> DuplicateVerdict:
        if not candidates:
            return DuplicateVerdict(status=STATUS_UNIQUE, score=None, reasons=[], matched_with=[])
        best_score = 0
        best_ids: list[UUID] = []
        best_reasons: list[str] = []
        for other in candidates:
            if other.id == contact.id:
                continue
            score, reasons = _agreement_score(contact, other)
            if score > best_score:
                best_score = score
                best_ids = [other.id]
                best_reasons = reasons
            elif score == best_score and score > 0:
                best_ids.append(other.id)
        if best_score >= settings.validation_duplicate_definite_min:
            status = STATUS_DUPLICATE
        elif best_score >= settings.validation_duplicate_possible_min:
            status = STATUS_POSSIBLE_DUPLICATE
        else:
            status = STATUS_UNIQUE
            best_ids = []
        return DuplicateVerdict(
            status=status, score=best_score or None, reasons=best_reasons, matched_with=best_ids
        )
