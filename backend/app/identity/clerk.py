"""Clerk session-token verification (System A).

Verifies Clerk-issued RS256 JWTs against the tenant's published JWKS without
a heavyweight JWT SDK dependency. Signing keys are cached with a TTL and are
refreshed when an unknown ``kid`` appears (key rotation). The verifier is
vitest-network-free by design: ``_fetch_jwks`` is a plain method so tests can
subclass or monkeypatch it without touching the network.

ClerkSaaSauth is only active when ``settings.clerk_issuer`` is configured; the
``get_verifier()`` factory returns ``None`` otherwise so callers can fall back
to the legacy authentication path.
"""

from __future__ import annotations

import time
from typing import Any

import jwt
import requests

from app.core.config import settings
from app.identity.schemas import VerifiedIdentity

JWKS_TTL_SECONDS = 3600
JWKS_FETCH_TIMEOUT_SECONDS = 10


class ClerkTokenError(ValueError):
    """A Clerk token failed cryptographic verification."""


class ClerkNotConfiguredError(ClerkTokenError):
    """Clerk integration is not configured (no issuer in settings)."""


def _claim_email(claims: dict[str, Any]) -> str | None:
    for key in ("email", "email_address", "preferred_email"):
        value = claims.get(key)
        if isinstance(value, str) and "@" in value:
            return value.lower()
    pii = claims.get("pii")
    if isinstance(pii, dict):
        value = pii.get("email")
        if isinstance(value, str) and "@" in value:
            return value.lower()
    return None


def _claim_display_name(claims: dict[str, Any]) -> str | None:
    for key in ("name", "display_name"):
        value = claims.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip()
    first = claims.get("first_name")
    last = claims.get("last_name")
    combined = " ".join(part for part in (first, last) if isinstance(part, str) and part.strip())
    if combined.strip():
        return combined.strip()
    username = claims.get("username")
    if isinstance(username, str) and username.strip():
        return username.strip()
    return None


class ClerkJWTVerifier:
    """Verifies Clerk session JWTs for one Clerk instance (issuer)."""

    def __init__(self, issuer: str, jwks_url: str | None = None, audience: str | None = None) -> None:
        self.issuer = issuer.rstrip("/")
        self.jwks_url = jwks_url or f"{self.issuer}/.well-known/jwks.json"
        self.audience = audience
        self._keys: list[dict[str, Any]] = []
        self._fetched_at = 0.0

    def _fetch_jwks(self) -> list[dict[str, Any]]:
        response = requests.get(self.jwks_url, timeout=JWKS_FETCH_TIMEOUT_SECONDS)
        response.raise_for_status()
        return list(response.json().get("keys") or [])

    def _signing_keys(self) -> list[dict[str, Any]]:
        now = time.monotonic()
        if not self._keys or (now - self._fetched_at) > JWKS_TTL_SECONDS:
            self._keys = self._fetch_jwks()
            self._fetched_at = now
        return self._keys

    def _public_key_for_kid(self, kid: str) -> Any:
        for key in self._signing_keys():
            if key.get("kid") == kid:
                return jwt.algorithms.RSAAlgorithm.from_jwk(key)
        # Unknown kid: Clerk may have rotated keys since our cache was fetched.
        self._keys = self._fetch_jwks()
        self._fetched_at = time.monotonic()
        for key in self._keys:
            if key.get("kid") == kid:
                return jwt.algorithms.RSAAlgorithm.from_jwk(key)
        raise ClerkTokenError("Signing key for the presented token was not found")

    def verify(self, token: str) -> VerifiedIdentity:
        if not token:
            raise ClerkTokenError("A bearer token is required")
        try:
            header = jwt.get_unverified_header(token)
        except jwt.PyJWTError as exc:
            raise ClerkTokenError("Token header is invalid") from exc
        kid = header.get("kid")
        if not kid:
            raise ClerkTokenError("Token is missing a key id")
        try:
            public_key = self._public_key_for_kid(kid)
        except ClerkTokenError:
            raise
        except requests.RequestException as exc:
            raise ClerkTokenError("Unable to resolve token signing keys") from exc
        except Exception as exc:
            raise ClerkTokenError("Unable to resolve token signing key") from exc

        options: dict[str, Any] = {"require": ["sub", "exp", "iss", "iat"]}
        decode_kwargs: dict[str, Any] = {"options": options, "issuer": self.issuer}
        if self.audience:
            decode_kwargs["audience"] = self.audience
        try:
            claims = jwt.decode(token, public_key, algorithms=["RS256"], **decode_kwargs)
        except jwt.ExpiredSignatureError as exc:
            raise ClerkTokenError("Token has expired") from exc
        except jwt.InvalidIssuerError as exc:
            raise ClerkTokenError("Token issuer does not match this Clerk instance") from exc
        except jwt.InvalidAudienceError as exc:
            raise ClerkTokenError("Token audience does not match") from exc
        except jwt.PyJWTError as exc:
            raise ClerkTokenError("Token signature or claims are invalid") from exc

        sub = str(claims.get("sub") or "")
        if not sub:
            raise ClerkTokenError("Token is missing a subject")
        return VerifiedIdentity(
            external_identity_id=sub,
            identity_provider="CLERK",
            email=_claim_email(claims),
            display_name=_claim_display_name(claims),
            token_claims=claims,
        )


_verifier: ClerkJWTVerifier | None = None


def get_verifier() -> ClerkJWTVerifier | None:
    """Return the shared configured verifier, or ``None`` if Clerk is off."""
    global _verifier
    if not settings.clerk_issuer:
        return None
    if _verifier is None:
        _verifier = ClerkJWTVerifier(
            issuer=settings.clerk_issuer,
            jwks_url=settings.clerk_jwks_url or None,
            audience=settings.clerk_audience,
        )
    return _verifier