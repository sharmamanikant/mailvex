"""SMTP recipient probing as an independent validation signal.

What this signal is
-------------------
An SMTP ``RCPT TO`` response is a *technical mail-server* signal. Per spec it
must never be reported as proof that a real person exists:

* ``ACCEPTED`` means the receiving server did not reject the address at the RCPT
  stage. It does **not** prove the mailbox exists or belongs to a person.
* ``CATCH_ALL`` means the domain accepted a randomly generated control address,
  so ``ACCEPTED`` for that domain carries no information at all.

Operational safeguards (all required by spec section 5)
-------------------------------------------------------
* Disabled by default (``VALIDATION_SMTP_ENABLED``). Environments that block
  outbound MX traffic get honest ``TIMEOUT``/``UNKNOWN`` values, never a
  fabricated pass.
* Per-run probe ceiling so a million-contact job cannot become a port scan.
* Redis circuit breaker: after N consecutive failures a host is skipped for a
  cooldown window rather than burning the whole run's budget on a dead server.
* ``domain_status``/``mx_status`` must already be positive - there is no point
  dialling a host we know cannot resolve or has no MX record.
"""

from __future__ import annotations

import logging
import random
import smtplib
import string
from dataclasses import dataclass

from app.core.config import settings
from app.services import validation_cache
from app.services.email_validation import DomainResolution, EmailSignals

logger = logging.getLogger(__name__)

SMTP_ACCEPTED = "ACCEPTED"
SMTP_REJECTED = "REJECTED"
SMTP_TEMPORARY_FAILURE = "TEMPORARY_FAILURE"
SMTP_TIMEOUT = "TIMEOUT"
SMTP_UNKNOWN = "UNKNOWN"
SMTP_CATCH_ALL = "CATCH_ALL"
SMTP_SKIPPED = "NOT_PROBED"

_PROBE_HELO = "verify.crcrm.local"
_PROBE_SENDER = "verify@crcrm.local"


@dataclass
class SmtpResult:
    status: str
    code: int | None = None
    message: str | None = None
    host: str | None = None
    catch_all: bool | None = None


def _control_address(domain: str) -> str:
    token = "".join(random.choices(string.ascii_lowercase + string.digits, k=16))
    return f"{token}@{domain}"


class SmtpProber:
    """Probes a single address, honouring the circuit breaker and run budget."""

    def __init__(self, max_probes: int | None = None) -> None:
        self._max_probes = (
            settings.validation_smtp_max_probes_per_run if max_probes is None else max_probes
        )
        self._used = 0
        self._failures: dict[str, int] = {}
        self._catch_all_cache: dict[str, bool | None] = {}

    @property
    def budget_remaining(self) -> int:
        return max(0, self._max_probes - self._used)

    @property
    def enabled(self) -> bool:
        return settings.validation_smtp_enabled

    def probe(self, email: str, signals: EmailSignals, resolution: DomainResolution) -> SmtpResult:
        if not self.enabled:
            return SmtpResult(status=SMTP_SKIPPED)
        if not signals.syntax_ok:
            return SmtpResult(status=SMTP_REJECTED, code=550, message="Invalid address syntax")
        host = resolution.mx_host
        if not host or resolution.mx_status != "VALID":
            return SmtpResult(
                status=SMTP_SKIPPED, message="No usable MX record for an SMTP probe"
            )
        if self._used >= self._max_probes:
            return SmtpResult(status=SMTP_SKIPPED, message="Per-run SMTP probe budget exhausted")
        if validation_cache.circuit_open(host):
            return SmtpResult(status=SMTP_SKIPPED, host=host, message="Host circuit breaker open")

        catch_all = self._detect_catch_all(host, signals.domain)
        self._used += 1
        result = self._rcpt(host, email)
        result.catch_all = catch_all
        if catch_all is True and result.status == SMTP_ACCEPTED:
            result.status = SMTP_CATCH_ALL
        self._record_outcome(host, result.status)
        return result

    def _rcpt(self, host: str, email: str) -> SmtpResult:
        timeout = settings.validation_smtp_timeout_seconds
        client: smtplib.SMTP | None = None
        try:
            client = smtplib.SMTP(host, 25, timeout=timeout)
            client.ehlo(_PROBE_HELO)
            code, message = client.mail(_PROBE_SENDER)
            if code >= 400:
                return SmtpResult(
                    status=SMTP_TEMPORARY_FAILURE,
                    code=code,
                    message=message.decode(errors="replace"),
                    host=host,
                )
            code, message = client.rcpt(email)
            text = message.decode(errors="replace")
            if 200 <= code < 300:
                return SmtpResult(status=SMTP_ACCEPTED, code=code, message=text, host=host)
            if code in (450, 451, 452):
                return SmtpResult(status=SMTP_TEMPORARY_FAILURE, code=code, message=text, host=host)
            if code in (550, 551, 553, 554):
                return SmtpResult(status=SMTP_REJECTED, code=code, message=text, host=host)
            return SmtpResult(status=SMTP_UNKNOWN, code=code, message=text, host=host)
        except TimeoutError:
            return SmtpResult(status=SMTP_TIMEOUT, host=host, message="Connection timed out")
        except smtplib.SMTPServerDisconnected:
            return SmtpResult(status=SMTP_TEMPORARY_FAILURE, host=host, message="Server disconnected")
        except (smtplib.SMTPException, OSError) as exc:
            return SmtpResult(
                status=SMTP_UNKNOWN, host=host, message=f"{type(exc).__name__}: {exc}"
            )
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _detect_catch_all(self, host: str, domain: str) -> bool | None:
        """A domain that accepts a random control address is catch-all."""
        if domain in self._catch_all_cache:
            return self._catch_all_cache[domain]
        if not domain or self._used >= self._max_probes:
            return None
        self._used += 1
        control = self._rcpt(host, _control_address(domain))
        verdict: bool | None
        if control.status == SMTP_ACCEPTED:
            verdict = True
        elif control.status == SMTP_REJECTED:
            verdict = False
        else:
            verdict = None
        self._catch_all_cache[domain] = verdict
        return verdict

    def _record_outcome(self, host: str, status: str) -> None:
        if status in (SMTP_TIMEOUT, SMTP_TEMPORARY_FAILURE):
            self._failures[host] = self._failures.get(host, 0) + 1
            validation_cache.record_smtp_outcome(
                host,
                self._failures[host],
                settings.validation_smtp_circuit_breaker_threshold,
                settings.validation_smtp_circuit_breaker_cooldown_seconds,
            )
        else:
            self._failures[host] = 0
            validation_cache.clear_circuit(host)
