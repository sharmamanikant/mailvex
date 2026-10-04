"""DNS resolution for sender health checks.

Uses ``dnspython`` behind a tiny protocol so checks can run against a static
resolver in tests (no live DNS) and so every caller handles the same
``DnsLookupResult`` shape. Lookups never raise: error conditions are
normalized to an ``error`` code on the result so a DNS outage degrades a check
to UNKNOWN instead of crashing the evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol

import dns.exception
import dns.rcode
import dns.resolver

DNS_TIMEOUT_SECONDS = 5.0


@dataclass(frozen=True)
class DnsLookupResult:
    """Outcome of a single DNS query.

    ``records`` carries the textual answers (TXT strings / MX hostnames).
    ``error`` is None on success (including "no records of this type"), or one
    of ``TMEOUT``/``NXDOMAIN``/``SERVFAIL``/``OTHER`` on failure.
    """

    records: tuple[str, ...]
    error: str | None = None


class DnsResolver(Protocol):
    def resolve_txt(self, name: str) -> DnsLookupResult: ...
    def resolve_mx(self, name: str) -> DnsLookupResult: ...


class SystemDnsResolver:
    """Live resolver backed by ``dns.resolver``."""

    def __init__(self, timeout_seconds: float = DNS_TIMEOUT_SECONDS) -> None:
        self.timeout_seconds = timeout_seconds

    def resolve_txt(self, name: str) -> DnsLookupResult:
        return self._resolve(name, "TXT")

    def resolve_mx(self, name: str) -> DnsLookupResult:
        return self._resolve(name, "MX")

    def _resolve(self, name: str, rtype: str) -> DnsLookupResult:
        try:
            answers = dns.resolver.resolve(name, rtype, lifetime=self.timeout_seconds)
            if rtype == "MX":
                records = tuple(str(item.exchange).rstrip(".") for item in answers)
            else:
                records = tuple(answer.to_text().strip('"') for answer in answers)
            return DnsLookupResult(records or ("",))
        except dns.resolver.NXDOMAIN:
            return DnsLookupResult((), error="NXDOMAIN")
        except dns.resolver.NoAnswer:
            # Domain resolves but has no records of this type.
            return DnsLookupResult(())
        except dns.resolver.NoNameservers as exc:
            rcode = getattr(exc, "rcode", None)
            if callable(rcode) and rcode() == dns.rcode.SERVFAIL:
                return DnsLookupResult((), error="SERVFAIL")
            return DnsLookupResult((), error="OTHER")
        except dns.exception.Timeout:
            return DnsLookupResult((), error="TIMEOUT")
        except (dns.exception.DNSException, OSError):
            return DnsLookupResult((), error="OTHER")


class StaticDnsResolver:
    """Deterministic resolver for tests and offline environments.

    ``txt`` maps names to TXT strings; ``mx`` maps names to MX hostnames;
    ``errors`` maps names to a resolver error code (e.g. ``TIMEOUT``).
    """

    def __init__(
        self,
        *,
        txt: dict[str, list[str]] | None = None,
        mx: dict[str, list[str]] | None = None,
        errors: dict[str, str] | None = None,
    ) -> None:
        self.txt = txt or {}
        self.mx = mx or {}
        self.errors = errors or {}

    def resolve_txt(self, name: str) -> DnsLookupResult:
        error = self.errors.get(name)
        if error:
            return DnsLookupResult((), error=error)
        return DnsLookupResult(tuple(self.txt.get(name, [])))

    def resolve_mx(self, name: str) -> DnsLookupResult:
        error = self.errors.get(name)
        if error:
            return DnsLookupResult((), error=error)
        return DnsLookupResult(tuple(self.mx.get(name, [])))