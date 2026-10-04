"""Domain extraction for sender health checks.

The engine only queries DNS for the domain embedded in the Sender's own email
address, and that email was tenant-validated when the Sender was created. We
never resolve arbitrary caller-supplied hostnames, which keeps the engine free
of SSRF-style delegation.
"""

from __future__ import annotations

import re

# Roughly a single lowercase ASCII (IDNA) hostname: labels of 1-63 chars,
# overall <= 253 chars, optional trailing dot.
DOMAIN_RE = re.compile(
    r"^(?=.{1,253}$)"
    r"(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)+"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$"
)


def extract_domain(email: str | None) -> str | None:
    """Normalize the sender email into its ASCII (IDNA) domain, or None.

    Rules:
      * whitespace is stripped; empty / ``@``-less / multi-``@`` inputs -> None
      * a trailing dot on the domain is ignored
      * internationalized domains are converted to punycode
      * the result must match ``DOMAIN_RE`` and be at most 253 chars

    Callers treat ``None`` as a DOMAIN check FAIL, not an UNKNOWN lookup.
    """
    if not email:
        return None
    cleaned = email.strip()
    parts = cleaned.rsplit("@", 1)
    if len(parts) != 2 or not parts[0] or not parts[1]:
        return None
    raw_domain = parts[1].strip().rstrip(".")
    if not raw_domain:
        return None
    if re.search(r"\s", raw_domain):
        return None
    try:
        ascii_domain = raw_domain.encode("idna").decode("ascii").lower()
    except UnicodeError:
        return None
    if len(ascii_domain) > 253:
        return None
    if not DOMAIN_RE.match(ascii_domain):
        return None
    return ascii_domain.lower()