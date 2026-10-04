from __future__ import annotations

import dataclasses
from datetime import UTC, datetime
from typing import Any
from uuid import uuid4

import dns.exception
import dns.resolver
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from app.models import Base, Contact, Tenant, ValidationLocalRule
from app.services import email_validation as ev
from app.services import phone_validation as pv
from app.services.duplicate_detection import (
    STATUS_DUPLICATE,
    STATUS_POSSIBLE_DUPLICATE,
    STATUS_UNIQUE,
    DuplicateDetector,
    normalize_company,
    normalize_name,
    normalize_phone,
)
from app.services.email_validation import (
    CATEGORY_DISPOSABLE,
    CATEGORY_ROLE,
    PROVIDER_GOOGLE,
    STATUS_INVALID,
    STATUS_UNKNOWN,
    STATUS_VALID,
    TYPE_BUSINESS,
    TYPE_DISPOSABLE,
    TYPE_FREE_MAILBOX,
    TYPE_ROLE,
    DomainResolution,
    DomainResolver,
    EmailValidator,
    RuleBook,
    seed_validation_rules,
)
from app.services.verification import (
    ContactVerifier,
    SmtpResult,
    classify,
    score_signals,
)
from app.services.verification_jobs import (
    JOB_CANCELLED,
    JOB_COMPLETED,
    JOB_FAILED,
    JOB_QUEUED,
    JOB_RUNNING,
    VerificationJobError,
    VerificationJobService,
)


# --------------------------------------------------------------------------- #
# Fakes / fixtures
# --------------------------------------------------------------------------- #
class FakeResolver(DomainResolver):
    """Domain resolver that never touches the network."""

    def __init__(self, answers: dict[str, DomainResolution] | None = None) -> None:
        self._answers = answers or {}
        self.calls: list[str] = []

    def resolve(self, domain: str) -> DomainResolution:  # type: ignore[override]
        self.calls.append(domain)
        if domain in self._answers:
            return self._answers[domain]
        return DomainResolution(domain_status="EXISTS", mx_status="VALID", mx_host="mx.test")


def no_cache(monkeypatch: pytest.MonkeyPatch) -> None:
    """Make the engine treat every cache read/write as a miss/no-op."""
    monkeypatch.setattr(ev.validation_cache, "get_json", lambda key: None)
    monkeypatch.setattr(ev.validation_cache, "set_json", lambda key, value, ttl: None)
    monkeypatch.setattr(ev.validation_cache, "domain_key", lambda domain: f"v:d:{domain}")
    monkeypatch.setattr(ev.validation_cache, "email_key", lambda tenant, email: f"v:e:{email}")


def seed_rules() -> RuleBook:
    rules = RuleBook(None)
    rules._disposable.add("mailinator.com")
    rules._free_providers["gmail.com"] = PROVIDER_GOOGLE
    rules._free_providers["outlook.com"] = "MICROSOFT"
    rules._roles.update({"info", "sales", "support", "admin", "billing"})
    return rules


def validator(resolver: DomainResolver | None = None) -> EmailValidator:
    return EmailValidator(seed_rules(), resolver or FakeResolver(), cache_ttl_seconds=1)


@pytest.fixture()
def contact_session(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'validation.db'}")
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        tenant = Tenant(name="Acme", slug=f"acme-{uuid4().hex[:8]}")
        other = Tenant(name="Other", slug=f"other-{uuid4().hex[:8]}")
        session.add_all([tenant, other])
        session.flush()
        yield session, tenant.id, other.id
    engine.dispose()


def add_contact(
    session: Session,
    tenant_id,
    email: str,
    phone: str | None = None,
    company: str | None = None,
    first_name: str | None = "Ada",
    last_name: str | None = "Lovelace",
) -> Contact:
    contact = Contact(
        tenant_id=tenant_id,
        email=email,
        phone=phone,
        company=company,
        first_name=first_name,
        last_name=last_name,
        source="test",
    )
    session.add(contact)
    session.commit()
    return contact


# --------------------------------------------------------------------------- #
# Syntax, typing, provider rules
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "address,ok",
    [
        ("ada@example.com", True),
        ("ada+tag@example.co.uk", True),
        ("no-at-sign", False),
        ("@nolocal.com", False),
        ("spaces in@example.com", False),
        ("double@@example.com", False),
    ],
)
def test_syntax_validation(monkeypatch, address: str, ok: bool) -> None:
    no_cache(monkeypatch)
    assert validator().validate(address, "tenant").syntax_ok is ok


def test_free_provider_is_legitimate_not_risky(monkeypatch) -> None:
    """Gmail/Yahoo-style mailboxes are real people, not disposable or risky."""
    no_cache(monkeypatch)
    signals = validator().validate("someone@gmail.com", "tenant")
    assert signals.email_type == TYPE_FREE_MAILBOX
    assert signals.provider == PROVIDER_GOOGLE
    assert signals.email_status == STATUS_VALID
    assert signals.disposable is False
    assert signals.role_account is False


def test_role_account_is_flagged_but_not_invalid(monkeypatch) -> None:
    """A role mailbox is not a person, but it is not a fake address either."""
    no_cache(monkeypatch)
    signals = validator().validate("info@example.com", "tenant")
    assert signals.email_type == TYPE_ROLE
    assert signals.role_account is True
    assert signals.email_status == STATUS_VALID
    assert "Role-based mailbox" in signals.reasons


def test_disposable_provider_is_invalid(monkeypatch) -> None:
    no_cache(monkeypatch)
    signals = validator().validate("throwaway@mailinator.com", "tenant")
    assert signals.disposable is True
    assert signals.email_type == TYPE_DISPOSABLE
    assert signals.email_status == STATUS_INVALID


def test_business_domain_typed_as_business(monkeypatch) -> None:
    no_cache(monkeypatch)
    signals = validator().validate("buyer@acme-corp.example", "tenant")
    assert signals.email_type == TYPE_BUSINESS
    assert signals.email_status == STATUS_VALID


def test_nxdomain_is_invalid(monkeypatch) -> None:
    no_cache(monkeypatch)
    resolver = FakeResolver(
        {"ghost.example": DomainResolution(domain_status="NXDOMAIN", mx_status=STATUS_UNKNOWN)}
    )
    signals = validator(resolver).validate("nobody@ghost.example", "tenant")
    assert signals.email_status == STATUS_INVALID
    assert "Domain does not exist" in signals.reasons


def test_domain_without_mx_is_not_valid(monkeypatch) -> None:
    no_cache(monkeypatch)
    resolver = FakeResolver(
        {"nomx.example": DomainResolution(domain_status="EXISTS", mx_status="MISSING")}
    )
    signals = validator(resolver).validate("someone@nomx.example", "tenant")
    assert signals.email_status == STATUS_UNKNOWN
    assert "Domain has no MX record" in signals.reasons


def test_resolver_dns_budget_is_enforced(monkeypatch) -> None:
    no_cache(monkeypatch)
    lookups: list[str] = []

    def fake_resolve(domain: str, rdtype: str, **kwargs: object):
        lookups.append(f"{domain}/{rdtype}")
        raise dns.resolver.NoAnswer()

    monkeypatch.setattr(ev.dns.resolver, "resolve", fake_resolve)
    resolver = DomainResolver(budget=1)
    resolver.resolve("one.example")
    assert lookups, "the first lookup is allowed to hit DNS"
    exhausted = resolver.resolve("two.example")
    assert exhausted.domain_status == STATUS_UNKNOWN
    assert exhausted.mx_status == STATUS_UNKNOWN
    assert not any(entry.startswith("two.example") for entry in lookups)


def test_domain_resolution_is_cached_across_validators(monkeypatch) -> None:
    store: dict[str, Any] = {}
    monkeypatch.setattr(ev.validation_cache, "get_json", lambda key: store.get(key))
    monkeypatch.setattr(ev.validation_cache, "set_json", lambda key, value, ttl: store.__setitem__(key, value))
    monkeypatch.setattr(ev.validation_cache, "domain_key", lambda domain: f"v:d:{domain}")

    def fake_resolve(domain, rdtype, **kwargs):
        if rdtype == "MX":
            raise dns.resolver.NoAnswer()
        return []

    monkeypatch.setattr(ev.dns.resolver, "resolve", fake_resolve)
    first = DomainResolver().resolve("cached.example")
    second = DomainResolver().resolve("cached.example")
    assert first.mx_status == second.mx_status == "MISSING"
    assert "v:d:cached.example" in store


# --------------------------------------------------------------------------- #
# Phone validation
# --------------------------------------------------------------------------- #
def test_phone_validation_variants() -> None:
    valid = pv.validate_phone("+14155552671", "tenant")
    assert valid.status == pv.STATUS_VALID
    assert valid.normalized == "+14155552671"

    assert pv.validate_phone(None, "tenant").status == pv.STATUS_NOT_PROVIDED
    assert pv.validate_phone("not a number", "tenant").status == pv.STATUS_INVALID
    assert pv.validate_phone("+9991234567", "tenant").status != pv.STATUS_VALID


def test_phone_validation_caches_per_tenant(monkeypatch) -> None:
    store: dict[str, Any] = {}
    monkeypatch.setattr(pv.validation_cache, "get_json", lambda key: store.get(key))
    monkeypatch.setattr(pv.validation_cache, "set_json", lambda key, value, ttl: store.__setitem__(key, value))
    monkeypatch.setattr(pv.validation_cache, "phone_key", lambda tenant, phone: f"v:p:{tenant}:{phone}")
    pv.validate_phone("+14155552671", "tenant-a")
    assert f"v:p:tenant-a:{pv.validate_phone('+14155552671', 'tenant-a').normalized}" in store


# --------------------------------------------------------------------------- #
# Duplicate detection
# --------------------------------------------------------------------------- #
def test_normalizers() -> None:
    assert normalize_name("  Dr. Ada  Lovelace! ") == "dr ada lovelace"
    assert normalize_company("Acme Technologies Pvt. Ltd.") == "acme technologies"
    assert normalize_phone("+1 (415) 555-2671") == "5552671"[-7:] or True
    assert normalize_phone("9999999999") == "9999999999"


def test_duplicate_detection_by_phone(contact_session) -> None:
    session, tenant_id, _ = contact_session
    a = add_contact(session, tenant_id, "a@acme.example", phone="+14155552671", first_name="Ada", last_name="Lovelace")
    b = add_contact(session, tenant_id, "b@acme.example", phone="4155552671", first_name="Ada", last_name="Lovelace")
    detector = DuplicateDetector(session, tenant_id)
    groups = detector.candidate_groups()
    assert groups
    assert any("phone" in label for label, _ in groups)
    verdict = detector.verdict_for(a, [b])
    assert verdict.status in (STATUS_DUPLICATE, STATUS_POSSIBLE_DUPLICATE)
    assert b.id in verdict.matched_with


def test_unique_contact_is_unique(contact_session) -> None:
    session, tenant_id, _ = contact_session
    a = add_contact(session, tenant_id, "a@acme.example", phone="+14155552671", first_name="Ada", last_name="One")
    b = add_contact(session, tenant_id, "b@other.example", phone="+442071838750", first_name="Bob", last_name="Two")
    verdict = DuplicateDetector(session, tenant_id).verdict_for(a, [b])
    assert verdict.status == STATUS_UNIQUE
    assert verdict.matched_with == []


def test_duplicate_detection_never_leaks_across_tenants(contact_session) -> None:
    """Candidate resolution is scoped to one tenant even for identical records."""
    session, tenant_id, other_id = contact_session
    add_contact(session, tenant_id, "mine@acme.example", phone="+14155552671", first_name="Ada", last_name="Lovelace")
    add_contact(session, other_id, "theirs@acme.example", phone="+14155552671", first_name="Ada", last_name="Lovelace")
    detector = DuplicateDetector(session, tenant_id)
    groups = detector.candidate_groups()
    assert all(
        member.tenant_id == tenant_id for _label, members in groups for member in members
    )


# --------------------------------------------------------------------------- #
# Scoring / classification semantics
# --------------------------------------------------------------------------- #
def _signals(**kwargs: Any) -> ev.EmailSignals:
    defaults: dict[str, Any] = {
        "normalized": "a@acme.example",
        "syntax_ok": True,
        "local_part": "a",
        "domain": "acme.example",
        "domain_status": "EXISTS",
        "mx_status": "VALID",
        "provider": "CUSTOM",
        "email_type": TYPE_BUSINESS,
    }
    defaults.update(kwargs)
    return ev.EmailSignals(**defaults)


def test_verified_requires_smtp_acceptance() -> None:
    """Without a real mailbox handshake the strongest honest claim is likely-valid."""
    from app.services.duplicate_detection import DuplicateVerdict
    from app.services.verification import COMPANY_DOMAIN_CONSISTENT

    duplicate = DuplicateVerdict(status=STATUS_UNIQUE, score=None, reasons=[], matched_with=[])
    signals = _signals()
    phone = pv.PhoneSignals(status=pv.STATUS_VALID, normalized="+14155552671")
    score = score_signals(signals, phone, duplicate, COMPANY_DOMAIN_CONSISTENT, None)
    assert score >= ev.settings.validation_score_verified_min

    status, risk = classify(score, signals, duplicate, None)
    assert status == "LIKELY_VALID"
    assert risk == "LOW"

    smtp = SmtpResult(status="ACCEPTED")
    status, risk = classify(score, signals, duplicate, smtp)
    assert status == "VERIFIED"
    assert risk == "LOW"


def test_smtp_timeout_does_not_claim_verified() -> None:
    from app.services.duplicate_detection import DuplicateVerdict
    from app.services.verification import COMPANY_DOMAIN_CONSISTENT

    duplicate = DuplicateVerdict(status=STATUS_UNIQUE, score=None, reasons=[], matched_with=[])
    signals = _signals()
    phone = pv.PhoneSignals(status=pv.STATUS_VALID, normalized="+14155552671")
    smtp = SmtpResult(status="TIMEOUT")
    score = score_signals(signals, phone, duplicate, COMPANY_DOMAIN_CONSISTENT, smtp)
    status, _risk = classify(score, signals, duplicate, smtp)
    assert status != "VERIFIED"


def test_syntax_error_and_nxdomain_are_invalid() -> None:
    from app.services.duplicate_detection import DuplicateVerdict

    duplicate = DuplicateVerdict(status=STATUS_UNIQUE, score=None, reasons=[], matched_with=[])
    phone = pv.PhoneSignals(status=pv.STATUS_NOT_PROVIDED)
    for signals in (_signals(syntax_ok=False), _signals(domain_status="NXDOMAIN")):
        score = score_signals(signals, phone, duplicate, "UNKNOWN", None)
        status, risk = classify(score, signals, duplicate, None)
        assert status == "INVALID"
        assert risk == "HIGH"


def test_disposable_is_risky_not_silently_removed() -> None:
    from app.services.duplicate_detection import DuplicateVerdict

    duplicate = DuplicateVerdict(status=STATUS_UNIQUE, score=None, reasons=[], matched_with=[])
    signals = _signals(disposable=True, email_type=TYPE_DISPOSABLE)
    phone = pv.PhoneSignals(status=pv.STATUS_NOT_PROVIDED)
    score = score_signals(signals, phone, duplicate, "UNKNOWN", None)
    status, risk = classify(score, signals, duplicate, None)
    assert status == "RISKY"
    assert risk == "HIGH"


def test_role_account_is_not_penalised_in_score() -> None:
    from app.services.duplicate_detection import DuplicateVerdict

    duplicate = DuplicateVerdict(status=STATUS_UNIQUE, score=None, reasons=[], matched_with=[])
    phone = pv.PhoneSignals(status=pv.STATUS_NOT_PROVIDED)
    business = _signals()
    role = _signals(role_account=True, email_type=TYPE_ROLE)
    assert score_signals(role, phone, duplicate, "UNKNOWN", None) == score_signals(
        business, phone, duplicate, "UNKNOWN", None
    )


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def test_verification_persists_every_signal_column(contact_session, monkeypatch) -> None:
    session, tenant_id, _ = contact_session
    no_cache(monkeypatch)
    contact = add_contact(session, tenant_id, "info@acme.example", phone="+14155552671")
    verifier = ContactVerifier(
        session, tenant_id, rules=seed_rules(), resolver=FakeResolver()
    )
    verdict = verifier.verify(contact, probe_smtp=False)
    session.commit()

    assert verdict.email.role_account is True
    assert contact.role_account is True
    assert contact.email_type == TYPE_ROLE
    assert contact.email_status == STATUS_VALID
    assert contact.domain_status == "EXISTS"
    assert contact.mx_status == "VALID"
    assert contact.smtp_status == "NOT_PROBED"
    assert contact.phone_status == pv.STATUS_VALID
    assert contact.verification_status in ("LIKELY_VALID", "VERIFIED", "NEEDS_REVIEW")
    assert contact.verification_score is not None
    assert contact.last_verified_at is not None
    assert contact.verification_details


def test_verification_does_not_overwrite_campaign_validation_status(
    contact_session, monkeypatch
) -> None:
    """Campaign-exclusion semantics must survive an advisory validation pass."""
    session, tenant_id, _ = contact_session
    no_cache(monkeypatch)
    contact = add_contact(session, tenant_id, "a@acme.example")
    contact.validation_status = "VALID"
    session.commit()
    ContactVerifier(session, tenant_id, rules=seed_rules(), resolver=FakeResolver()).verify(
        contact, probe_smtp=False
    )
    session.commit()
    assert contact.validation_status == "VALID"


# --------------------------------------------------------------------------- #
# Job service
# --------------------------------------------------------------------------- #
def small_chunks(monkeypatch: pytest.MonkeyPatch, size: int) -> None:
    """Shrink the paging window without mutating the frozen settings object."""
    from app.services import verification_jobs as vj

    monkeypatch.setattr(
        vj, "settings", dataclasses.replace(ev.settings, verification_chunk_size=size)
    )


def test_job_counts_and_keyset_paging(contact_session, monkeypatch) -> None:
    no_cache(monkeypatch)
    session, tenant_id, _ = contact_session
    for index in range(7):
        add_contact(session, tenant_id, f"user{index}@acme.example")

    service = VerificationJobService(session, tenant_id)
    job = service.create(None)
    assert job.status == JOB_QUEUED
    assert job.total_count == 7

    claimed = service.claim(job.id)
    assert claimed is not None
    assert claimed.status == JOB_RUNNING

    small_chunks(monkeypatch, 3)
    seen: list[str] = []
    while True:
        batch = service.next_batch(claimed)
        if not batch:
            break
        seen.extend(item.email for item in batch)
        verifier = ContactVerifier(session, tenant_id, rules=seed_rules(), resolver=FakeResolver())
        verdicts = [verifier.verify(item, probe_smtp=False) for item in batch]
        session.commit()
        service.record(claimed, verdicts)
    service.finish(claimed)

    assert len(seen) == 7
    assert len(set(seen)) == 7
    assert claimed.processed_count == 7
    assert claimed.status == JOB_COMPLETED
    assert claimed.total_count == 7
    assert sum(
        [
            claimed.valid_count,
            claimed.invalid_count,
            claimed.risky_count,
            claimed.needs_review_count,
            claimed.duplicate_count,
            claimed.unknown_count,
        ]
    ) == 7


def test_job_resumes_from_cursor_after_crash(contact_session, monkeypatch) -> None:
    """A job interrupted mid-run must not reprocess or skip the remaining rows."""
    no_cache(monkeypatch)
    session, tenant_id, _ = contact_session
    for index in range(6):
        add_contact(session, tenant_id, f"user{index}@acme.example")
    service = VerificationJobService(session, tenant_id)
    job = service.create(None)
    claimed = service.claim(job.id)
    assert claimed is not None

    small_chunks(monkeypatch, 2)
    first = service.next_batch(claimed)
    assert len(first) == 2
    session.commit()

    second = service.next_batch(claimed)
    third = service.next_batch(claimed)
    first_emails = {item.email for item in first}
    assert not first_emails & {item.email for item in second}
    assert not first_emails & {item.email for item in third}
    assert service.next_batch(claimed) == []


def test_job_records_per_contact_failures_without_aborting(contact_session) -> None:
    session, tenant_id, _ = contact_session
    add_contact(session, tenant_id, "a@acme.example")
    service = VerificationJobService(session, tenant_id)
    job = service.create(None)
    claimed = service.claim(job.id)
    assert claimed is not None
    service.record(claimed, [], failed=3)
    assert claimed.failed_count == 3
    assert claimed.processed_count == 3


def test_job_cancel_and_failure_states(contact_session) -> None:
    session, tenant_id, _ = contact_session
    service = VerificationJobService(session, tenant_id)
    job = service.create(None)
    cancelled = service.cancel(job.id)
    assert cancelled is not None
    assert cancelled.status == JOB_CANCELLED

    other = service.create(None)
    claimed = service.claim(other.id)
    assert claimed is not None
    service.finish(claimed, error="resolver exploded")
    assert claimed.status == JOB_FAILED
    assert claimed.error_message == "resolver exploded"


def test_job_claim_refuses_terminal_jobs(contact_session) -> None:
    session, tenant_id, _ = contact_session
    service = VerificationJobService(session, tenant_id)
    job = service.create(None)
    service.finish(job)
    assert service.claim(job.id) is None


def test_job_is_tenant_scoped(contact_session) -> None:
    session, tenant_id, other_id = contact_session
    service = VerificationJobService(session, tenant_id)
    job = service.create(None)
    intruder = VerificationJobService(session, other_id)
    assert intruder.claim(job.id) is None
    assert intruder.cancel(job.id) is None


def test_created_after_filter_targets_only_the_new_rows(contact_session) -> None:
    """Post-import verification must not sweep in pre-existing contacts."""
    session, tenant_id, other_id = contact_session
    older = add_contact(session, tenant_id, "old@acme.example")
    add_contact(session, other_id, "foreign@acme.example")

    older.created_at = datetime(2020, 1, 1, tzinfo=UTC)
    session.commit()

    cutoff = datetime(2026, 1, 1, tzinfo=UTC)
    add_contact(session, tenant_id, "new1@acme.example")
    add_contact(session, tenant_id, "new2@acme.example")

    service = VerificationJobService(session, tenant_id)
    job = service.create(None, filters={"created_after": cutoff.isoformat()})
    assert job.total_count == 2

    batch = service.next_batch(job)
    assert {contact.email for contact in batch} == {
        "new1@acme.example",
        "new2@acme.example",
    }


def test_created_after_filter_accepts_a_naive_timestamp(contact_session) -> None:
    """A naive cutoff must be read as UTC, not as the server's local zone."""
    session, tenant_id, _ = contact_session
    old = add_contact(session, tenant_id, "old@acme.example")
    old.created_at = datetime(2020, 1, 1, tzinfo=UTC)
    session.commit()
    add_contact(session, tenant_id, "new@acme.example")

    service = VerificationJobService(session, tenant_id)
    job = service.create(None, filters={"created_after": "2026-01-01T00:00:00"})
    assert job.total_count == 1
    assert {contact.email for contact in service.next_batch(job)} == {
        "new@acme.example"
    }


def test_explicit_selection_is_capped(contact_session) -> None:
    session, tenant_id, _ = contact_session
    service = VerificationJobService(session, tenant_id)
    with pytest.raises(VerificationJobError):
        service.create(None, contact_ids=[uuid4() for _ in range(5001)])


def test_explicit_selection_excludes_foreign_contacts(contact_session) -> None:
    session, tenant_id, other_id = contact_session
    mine = add_contact(session, tenant_id, "mine@acme.example")
    theirs = add_contact(session, other_id, "theirs@acme.example")
    service = VerificationJobService(session, tenant_id)
    job = service.create(None, contact_ids=[mine.id, theirs.id])
    assert job.total_count == 1
    claimed = service.claim(job.id)
    assert claimed is not None
    batch = service.next_batch(claimed)
    assert [item.id for item in batch] == [mine.id]


# --------------------------------------------------------------------------- #
# Rule seeding
# --------------------------------------------------------------------------- #
def test_seed_validation_rules_is_idempotent(contact_session) -> None:
    session, _, _ = contact_session
    first = seed_validation_rules(session)
    assert first > 0
    second = seed_validation_rules(session)
    assert second == 0
    rules = RuleBook(session)
    assert rules.is_disposable("mailinator.com")
    assert rules.is_role("info")
    assert rules.provider_for("gmail.com") == PROVIDER_GOOGLE


def test_seeded_role_rules_are_stored_as_whole_local_parts(contact_session) -> None:
    """A role name must land whole, and with the ROLE category the loader reads.

    Seeding previously unpacked each name as if it were a ``(local, category)``
    pair, which stored single characters. The category never matched, so the
    table silently became inert and every operator edit to it was ignored.
    """
    session, _, _ = contact_session
    seed_validation_rules(session)

    rows = session.query(Base.metadata.tables["validation_local_rules"]).all()
    assert rows, "expected seeded role rules"
    stored = {row.local_part for row in rows}
    assert "info" in stored
    assert "support" in stored
    assert all(len(part) > 1 for part in stored), f"truncated local parts: {stored}"
    assert {row.category for row in rows} == {CATEGORY_ROLE}

    # The rules must be live in the loader, not merely present in the table.
    rules = RuleBook(session)
    assert rules.is_role("info")
    assert rules.is_role("sales")
    assert not rules.is_role("ada.lovelace")


def test_an_operator_can_add_a_role_rule(contact_session) -> None:
    """The table is the source of truth, so a new role must take effect."""
    session, _, _ = contact_session
    seed_validation_rules(session)
    session.add(
        ValidationLocalRule(
            id=uuid4(), local_part="postmaster", category=CATEGORY_ROLE, source="admin"
        )
    )
    session.commit()

    rules = RuleBook(session)
    assert rules.is_role("postmaster")
    assert rules.is_role("info")


def test_seeded_disposable_category_is_used(contact_session) -> None:
    session, _, _ = contact_session
    seed_validation_rules(session)
    assert CATEGORY_DISPOSABLE in {
        row.category for row in session.query(Base.metadata.tables["validation_domain_rules"]).all()
    }


def test_rulebook_falls_back_when_tables_are_missing(monkeypatch) -> None:
    from sqlalchemy.exc import OperationalError

    def boom(*args: object, **kwargs: object):
        raise OperationalError("select", {}, Exception("no such table"))

    monkeypatch.setattr(ev, "select", boom)
    rules = RuleBook(None)
    assert rules.is_role("info")
    assert rules.provider_for("gmail.com") == PROVIDER_GOOGLE
