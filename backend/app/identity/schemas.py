"""Identity schemas shared by the Clerk verification and mapping layers."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

IDENTITY_PROVIDER_CLERK = "CLERK"

PLACEHOLDER_EMAIL_DOMAIN = "identity.local"


@dataclass(frozen=True)
class VerifiedIdentity:
    """A cryptographically verified external identity (a Clerk session user).

    ``token_claims`` retains the decoded but *unsigned-verified* claim set for
    traceability; sensitive PII inside it must never be stored or logged.
    """

    external_identity_id: str
    identity_provider: str = IDENTITY_PROVIDER_CLERK
    email: str | None = None
    display_name: str | None = None
    token_claims: dict[str, Any] = field(default_factory=dict)

    @property
    def mapped_email(self) -> str:
        """Best-effort application email for this identity.

        Clerk's default session token does not always include an ``email``
        claim; when absent we emit a deterministic placeholder address so the
        mapping/provisioning flow still produces a stable, unique user.
        """
        email = (self.email or "").strip().lower()
        if email:
            return email
        return f"clerk-{self.external_identity_id}@{PLACEHOLDER_EMAIL_DOMAIN}"