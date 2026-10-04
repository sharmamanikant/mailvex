"""Email validation signals for the contact validation engine.

Pipeline order (per spec):

    normalize -> syntax -> domain -> DNS -> MX -> provider ->
    disposable -> role account -> [SMTP] -> catch-all -> result

Every stage is allowed to answer UNKNOWN. A stage that cannot be evaluated
(for example DNS when the resolver times out) must never be reported as a
pass or a fail, because both would be fabricated signals.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

import dns.exception
import dns.resolver
from sqlalchemy import select
from sqlalchemy.dialects import postgresql
from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models import ValidationDomainRule, ValidationLocalRule
from app.services import validation_cache

logger = logging.getLogger(__name__)

EMAIL_PATTERN = re.compile(r"^[^\s@]+@[^\s@]+\.[^\s@]+$")
EMAIL_MAX_LENGTH = 320
DOMAIN_PATTERN = re.compile(r"^(?=.{1,253}$)(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+[a-z]{2,63}$")

STATUS_VALID = "VALID"
STATUS_INVALID = "INVALID"
STATUS_UNKNOWN = "UNKNOWN"
STATUS_NOT_PROBED = "NOT_PROBED"

TYPE_FREE_MAILBOX = "FREE_MAILBOX"
TYPE_BUSINESS = "BUSINESS"
TYPE_ROLE = "ROLE"
TYPE_DISPOSABLE = "DISPOSABLE"
TYPE_UNKNOWN = "UNKNOWN"

PROVIDER_GOOGLE = "GOOGLE"
PROVIDER_MICROSOFT = "MICROSOFT"
PROVIDER_YAHOO = "YAHOO"
PROVIDER_APPLE = "APPLE"
PROVIDER_PROTON = "PROTON"
PROVIDER_CUSTOM = "CUSTOM"
PROVIDER_UNKNOWN = "UNKNOWN"

CATEGORY_DISPOSABLE = "DISPOSABLE"
CATEGORY_FREE_PROVIDER = "FREE_PROVIDER"
CATEGORY_ROLE = "ROLE"

# Curated seed data only. The table is the source of truth at runtime; this
# constant merely bootstraps it and keeps the engine working before seeding.
# Bulk disposable lists belong in ``validation_domain_rules``, not in source.
SEED_DOMAIN_RULES: tuple[tuple[str, str, str | None], ...] = tuple(
    (domain, CATEGORY_FREE_PROVIDER, provider)
    for domain, provider in (
        ("gmail.com", PROVIDER_GOOGLE),
        ("googlemail.com", PROVIDER_GOOGLE),
        ("outlook.com", PROVIDER_MICROSOFT),
        ("hotmail.com", PROVIDER_MICROSOFT),
        ("live.com", PROVIDER_MICROSOFT),
        ("msn.com", PROVIDER_MICROSOFT),
        ("passport.com", PROVIDER_MICROSOFT),
        ("yahoo.com", PROVIDER_YAHOO),
        ("yahoo.co.in", PROVIDER_YAHOO),
        ("yahoo.co.uk", PROVIDER_YAHOO),
        ("ymail.com", PROVIDER_YAHOO),
        ("rocketmail.com", PROVIDER_YAHOO),
        ("icloud.com", PROVIDER_APPLE),
        ("me.com", PROVIDER_APPLE),
        ("mac.com", PROVIDER_APPLE),
        ("protonmail.com", PROVIDER_PROTON),
        ("proton.me", PROVIDER_PROTON),
        ("pm.me", PROVIDER_PROTON),
    )
) + tuple((domain, CATEGORY_DISPOSABLE, None) for domain in (
    "10minutemail.com",
    "guerrillamail.com",
    "mailinator.com",
    "tempmail.com",
    "yopmail.com",
    "trashmail.com",
    "sharklasers.com",
    "getnada.com",
    "maildrop.cc",
    "dispostable.com",
    "fakeinbox.com",
    "throwawaymail.com",
    "temp-mail.org",
    "moakt.com",
    "mytemp.email",
))

SEED_LOCAL_RULES: tuple[str, ...] = (
    "admin",
    "administrator",
    "abuse",
    "billing",
    "careers",
    "contact",
    "enquiries",
    "enquiry",
    "finance",
    "hello",
    "hr",
    "info",
    "invoices",
    "jobs",
    "legal",
    "mail",
    "marketing",
    "media",
    "no-reply",
    "noreply",
    "office",
    "orders",
    "press",
    "recruitment",
    "sales",
    "security",
    "support",
    "team",
    "webmaster",
)


@dataclass
class EmailSignals:
    """Raw, per-address technical signals. No scoring happens here."""

    normalized: str
    syntax_ok: bool
    local_part: str
    domain: str
    domain_status: str = STATUS_UNKNOWN
    mx_status: str = STATUS_UNKNOWN
    mx_host: str | None = None
    provider: str = PROVIDER_UNKNOWN
    email_type: str = TYPE_UNKNOWN
    disposable: bool = False
    role_account: bool = False
    catch_all: bool | None = None
    smtp_status: str = STATUS_NOT_PROBED
    reasons: list[str] = field(default_factory=list)

    @property
    def email_status(self) -> str:
        if not self.syntax_ok:
            return STATUS_INVALID
        if self.disposable:
            return STATUS_INVALID
        if self.domain_status == "NXDOMAIN":
            return STATUS_INVALID
        if self.mx_status == "VALID":
            return STATUS_VALID
        return STATUS_UNKNOWN

    def as_dict(self) -> dict[str, Any]:
        return {
            "normalized": self.normalized,
            "syntax_ok": self.syntax_ok,
            "domain": self.domain,
            "domain_status": self.domain_status,
            "mx_status": self.mx_status,
            "mx_host": self.mx_host,
            "provider": self.provider,
            "email_type": self.email_type,
            "disposable": self.disposable,
            "role_account": self.role_account,
            "catch_all": self.catch_all,
            "smtp_status": self.smtp_status,
            "reasons": list(self.reasons),
        }


@dataclass
class DomainResolution:
    domain_status: str
    mx_status: str
    mx_host: str | None = None


def normalize_email(raw: str | None) -> str:
    if not raw:
        return ""
    candidate = raw.strip().strip("<>").strip()
    if not candidate:
        return ""
    if "@" not in candidate:
        return candidate.lower()
    local, _, domain = candidate.rpartition("@")
    return f"{local.strip().lower()}@{domain.strip().lower().rstrip('.')}"


def _syntax_is_valid(email: str) -> tuple[bool, str | None]:
    if not email or "@" not in email:
        return False, "Missing '@'"
    if len(email) > EMAIL_MAX_LENGTH:
        return False, f"Address exceeds {EMAIL_MAX_LENGTH} characters"
    local, _, domain = email.rpartition("@")
    if not local:
        return False, "Missing local part"
    if not domain:
        return False, "Missing domain"
    if not DOMAIN_PATTERN.match(domain):
        return False, "Domain is not well formed"
    if not EMAIL_PATTERN.match(email):
        return False, "Address is not well formed"
    return True, None


def seed_validation_rules(session: Session) -> int:
    """Persist the curated seed rules so they become admin-editable data.

    Idempotent: existing rows are left untouched, so re-running never clobbers
    an operator's edits. Returns the number of rows inserted.
    """
    domain_rows: list[dict[str, Any]] = [
        {
            "id": uuid4(),
            "domain": entry[0],
            "category": entry[1],
            "provider": entry[2],
            "source": "seed",
        }
        for entry in SEED_DOMAIN_RULES
    ]
    local_rows: list[dict[str, Any]] = [
        {
            "id": uuid4(),
            "local_part": name,
            "category": CATEGORY_ROLE,
            "source": "seed",
        }
        for name in SEED_LOCAL_RULES
    ]
    domain_insert: Any
    local_insert: Any
    if session.get_bind().dialect.name == "postgresql":
        domain_insert = postgresql.insert(ValidationDomainRule).values(domain_rows)
        domain_insert = domain_insert.on_conflict_do_nothing(
            index_elements=["domain", "category"]
        )
        local_insert = postgresql.insert(ValidationLocalRule).values(local_rows)
        local_insert = local_insert.on_conflict_do_nothing(
            index_elements=["local_part", "category"]
        )
    else:
        from sqlalchemy.dialects.sqlite import insert as sqlite_insert

        domain_insert = sqlite_insert(ValidationDomainRule).values(domain_rows)
        domain_insert = domain_insert.on_conflict_do_nothing(
            index_elements=["domain", "category"]
        )
        local_insert = sqlite_insert(ValidationLocalRule).values(local_rows)
        local_insert = local_insert.on_conflict_do_nothing(
            index_elements=["local_part", "category"]
        )
    session.commit()
    # RETURNING counts the rows that actually landed, skipping the conflicts.
    # ``rowcount`` is unusable here: a multi-row insert reports -1 ("unknown"),
    # which is truthy and would make this function report a negative count.
    inserted = len(session.execute(domain_insert.returning(ValidationDomainRule.id)).all())
    inserted += len(session.execute(local_insert.returning(ValidationLocalRule.id)).all())
    session.commit()
    return inserted


class RuleBook:
    """Loads disposable / free-provider / role-account rules from the database.

    Falls back to the curated seed constants when the tables are empty so a
    fresh database still validates correctly before seeding has run.
    """

    def __init__(self, session: Session | None = None) -> None:
        self._disposable: set[str] = set()
        self._free_providers: dict[str, str] = {}
        self._roles: set[str] = set()
        self._load(session)

    def _load(self, session: Session | None) -> None:
        if session is not None:
            try:
                domain_rows = session.scalars(select(ValidationDomainRule)).all()
                for row in domain_rows:
                    if row.category == CATEGORY_DISPOSABLE:
                        self._disposable.add(row.domain.lower())
                    elif row.category == CATEGORY_FREE_PROVIDER and row.provider:
                        self._free_providers[row.domain.lower()] = row.provider.upper()
                self._roles.update(
                    row.local_part.lower()
                    for row in session.scalars(select(ValidationLocalRule)).all()
                    if row.category == CATEGORY_ROLE
                )
            except SQLAlchemyError as exc:
                # Expected when the migration has not run yet. Log it rather
                # than swallowing silently so a real outage is visible.
                logger.warning("validation rule tables unavailable, using built-in seeds: %s", exc)
                session.rollback()
        if not self._disposable and not self._free_providers:
            for domain, category, provider in SEED_DOMAIN_RULES:
                if category == CATEGORY_DISPOSABLE:
                    self._disposable.add(domain)
                elif provider:
                    self._free_providers[domain] = provider
        if not self._roles:
            self._roles.update(SEED_LOCAL_RULES)

    def is_disposable(self, domain: str) -> bool:
        return domain.lower() in self._disposable

    def is_role(self, local_part: str) -> bool:
        return local_part.lower() in self._roles

    def provider_for(self, domain: str) -> str:
        return self._free_providers.get(domain.lower(), PROVIDER_CUSTOM)


class DomainResolver:
    """Cached DNS + MX resolution with a per-run unique-lookup budget."""

    def __init__(self, budget: int | None = None) -> None:
        self._budget = settings.validation_max_unique_dns_lookups if budget is None else budget
        self._resolved: dict[str, DomainResolution] = {}

    @property
    def budget_exhausted(self) -> bool:
        return self._budget <= 0

    def resolve(self, domain: str) -> DomainResolution:
        cached = self._resolved.get(domain)
        if cached is not None:
            return cached
        key = validation_cache.domain_key(domain)
        stored = validation_cache.get_json(key)
        if stored is not None:
            resolution = DomainResolution(
                domain_status=str(stored.get("domain_status", STATUS_UNKNOWN)),
                mx_status=str(stored.get("mx_status", STATUS_UNKNOWN)),
                mx_host=stored.get("mx_host"),
            )
            self._resolved[domain] = resolution
            return resolution
        if self._budget <= 0:
            resolution = DomainResolution(domain_status=STATUS_UNKNOWN, mx_status=STATUS_UNKNOWN)
            self._resolved[domain] = resolution
            return resolution
        self._budget -= 1
        resolution = self._lookup(domain)
        self._resolved[domain] = resolution
        validation_cache.set_json(
            key,
            {
                "domain_status": resolution.domain_status,
                "mx_status": resolution.mx_status,
                "mx_host": resolution.mx_host,
            },
            settings.validation_domain_cache_ttl_seconds,
        )
        return resolution

    def _lookup(self, domain: str) -> DomainResolution:
        lifetime = settings.validation_dns_timeout_seconds
        has_address = False
        try:
            dns.resolver.resolve(domain, "A", lifetime=lifetime)
            has_address = True
        except dns.resolver.NXDOMAIN:
            return DomainResolution(domain_status="NXDOMAIN", mx_status=STATUS_UNKNOWN)
        except (dns.resolver.NoAnswer, dns.resolver.NoNameservers):
            pass
        except (dns.exception.DNSException, OSError):
            pass
        try:
            records = dns.resolver.resolve(domain, "MX", lifetime=lifetime)
            hosts = sorted(records, key=lambda record: record.preference)
            if hosts:
                return DomainResolution(
                    domain_status="EXISTS",
                    mx_status="VALID",
                    mx_host=str(hosts[0].exchange).rstrip("."),
                )
        except dns.resolver.NXDOMAIN:
            return DomainResolution(domain_status="NXDOMAIN", mx_status=STATUS_UNKNOWN)
        except dns.resolver.NoAnswer:
            return DomainResolution(
                domain_status="EXISTS" if has_address else STATUS_UNKNOWN,
                mx_status="MISSING",
            )
        except (dns.exception.DNSException, OSError):
            return DomainResolution(domain_status=STATUS_UNKNOWN, mx_status=STATUS_UNKNOWN)
        return DomainResolution(
            domain_status="EXISTS" if has_address else STATUS_UNKNOWN,
            mx_status=STATUS_UNKNOWN,
        )


class EmailValidator:
    """Runs the full non-SMTP email pipeline for one address."""

    def __init__(
        self,
        rules: RuleBook,
        resolver: DomainResolver,
        cache_ttl_seconds: int | None = None,
    ) -> None:
        self._rules = rules
        self._resolver = resolver
        self._ttl = (
            settings.validation_email_cache_ttl_seconds
            if cache_ttl_seconds is None
            else cache_ttl_seconds
        )

    def validate(self, raw_email: str, tenant_id: str) -> EmailSignals:
        normalized = normalize_email(raw_email)
        cache_key = validation_cache.email_key(tenant_id, normalized)
        cached = validation_cache.get_json(cache_key)
        if cached is not None:
            return self._from_cache(normalized, cached)
        signals = self._compute(normalized)
        validation_cache.set_json(cache_key, signals.as_dict(), self._ttl)
        return signals

    def _from_cache(self, normalized: str, cached: dict[str, Any]) -> EmailSignals:
        local, _, domain = normalized.rpartition("@")
        return EmailSignals(
            normalized=normalized,
            syntax_ok=bool(cached.get("syntax_ok")),
            local_part=local,
            domain=domain,
            domain_status=str(cached.get("domain_status", STATUS_UNKNOWN)),
            mx_status=str(cached.get("mx_status", STATUS_UNKNOWN)),
            mx_host=cached.get("mx_host"),
            provider=str(cached.get("provider", PROVIDER_UNKNOWN)),
            email_type=str(cached.get("email_type", TYPE_UNKNOWN)),
            disposable=bool(cached.get("disposable")),
            role_account=bool(cached.get("role_account")),
            catch_all=cached.get("catch_all"),
            smtp_status=str(cached.get("smtp_status", STATUS_NOT_PROBED)),
            reasons=[str(item) for item in cached.get("reasons", [])],
        )

    def _compute(self, normalized: str) -> EmailSignals:
        local, _, domain = normalized.rpartition("@")
        ok, reason = _syntax_is_valid(normalized)
        if not ok:
            return EmailSignals(
                normalized=normalized,
                syntax_ok=False,
                local_part=local,
                domain=domain,
                reasons=[reason or "Invalid address"],
            )
        resolution = self._resolver.resolve(domain)
        disposable = self._rules.is_disposable(domain)
        role_account = self._rules.is_role(local)
        provider = self._rules.provider_for(domain)
        if disposable:
            email_type = TYPE_DISPOSABLE
        elif role_account:
            email_type = TYPE_ROLE
        elif provider != PROVIDER_CUSTOM:
            email_type = TYPE_FREE_MAILBOX
        else:
            email_type = TYPE_BUSINESS
        reasons: list[str] = []
        if resolution.domain_status == "NXDOMAIN":
            reasons.append("Domain does not exist")
        if resolution.mx_status == "MISSING":
            reasons.append("Domain has no MX record")
        if resolution.domain_status == STATUS_UNKNOWN:
            reasons.append("DNS could not be resolved")
        if disposable:
            reasons.append("Disposable email provider")
        if role_account:
            reasons.append("Role-based mailbox")
        return EmailSignals(
            normalized=normalized,
            syntax_ok=True,
            local_part=local,
            domain=domain,
            domain_status=resolution.domain_status,
            mx_status=resolution.mx_status,
            mx_host=resolution.mx_host,
            provider=provider,
            email_type=email_type,
            disposable=disposable,
            role_account=role_account,
            catch_all=None,
            reasons=reasons,
        )
