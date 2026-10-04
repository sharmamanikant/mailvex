"""Credential lifecycle for sender connections (System B).

Secrets are encrypted at rest via the shared Fernet ``CredentialStore`` and a
connection only ever stores an opaque ``credential_reference``. This module is
the ONLY boundary that can produce or decode a reference; API responses, audit
records and provider stubs never touch raw credentials.

Architecture pattern::

    sender_connection.credential_reference
        -> CredentialStore.encrypt()           (at write time)
        -> CredentialStore.decrypt()           (inside providers only)
        -> revoke/rotate produces a new reference

Versioning: each rotation bumps ``credential_version`` (e.g. ``v1`` -> ``v2``)
so a leaked older ciphertext can be retired without touching other tenants.
"""

from __future__ import annotations

from datetime import datetime

from app.security.credential_store import CredentialStore

__all__ = [
    "REVOKED_PREFIX",
    "CredentialPayload",
    "decrypt_credential_reference",
    "encrypt_credential_reference",
    "invalidate_credential_reference",
    "rotate_credential_reference",
]

REVOKED_PREFIX = "revoked:"


class CredentialPayload(dict[str, object]):
    """A validated credential payload accepted by the secure store."""


_SENSITIVE_KEYS = frozenset(
    {
        "api_key",
        "smtp_password",
        "client_secret",
        "access_token",
        "refresh_token",
    }
)


def validate_payload(payload: dict[str, object]) -> CredentialPayload:
    """Ensure a payload only carries known secret fields (rejects junk)."""
    allowed = {
        "api_key",
        "smtp_password",
        "client_secret",
        "access_token",
        "refresh_token",
        "smtp_username",
        "client_id",
        "tenant_id",
        "expires_at",
        "scopes",
        "token_uri",
    }
    unknown = set(payload) - allowed
    if unknown:
        raise ValueError(f"Unsupported credential field(s): {', '.join(sorted(unknown))}")
    if not any(key in payload for key in _SENSITIVE_KEYS):
        raise ValueError("Credential payload must contain at least one secret")
    return CredentialPayload(payload)


def encrypt_credential_reference(
    encryption_key: str,
    payload: dict[str, object],
    version: str = "v1",
) -> str:
    """Encrypt a payload and return an opaque reference for persistence."""
    validated = validate_payload(payload)
    body = dict(validated)
    body["credential_version"] = version
    return CredentialStore(encryption_key).encrypt(body)


def decrypt_credential_reference(
    encryption_key: str,
    credential_reference: str,
) -> CredentialPayload:
    """Decode a reference inside the provider boundary only."""
    if credential_reference.startswith(REVOKED_PREFIX):
        raise ValueError("Credential reference has been revoked")
    result = CredentialStore(encryption_key).decrypt(credential_reference)
    return CredentialPayload(result)


def rotate_credential_reference(
    encryption_key: str,
    credential_reference: str,
    new_payload: dict[str, object] | None = None,
    previous_version: str = "v1",
) -> tuple[str, str]:
    """Rotate a reference: re-encrypt (optionally with fresh secrets) under a
    bumped version. Returns ``(new_reference, new_version)``."""
    if new_payload is not None:
        payload = validate_payload(new_payload)
    else:
        payload = decrypt_credential_reference(encryption_key, credential_reference)
    try:
        next_version = future_version(previous_version)
    except ValueError:
        next_version = "v1"
    body = dict(payload)
    body["credential_version"] = next_version
    return CredentialStore(encryption_key).encrypt(body), next_version


def invalidate_credential_reference(credential_version: str) -> str:
    """Produce a tombstone reference so a connection can no longer decode
    credentials without deleting the row (enables audit of the revocation)."""
    return f"{REVOKED_PREFIX}v{credential_version}"


def future_version(version: str) -> str:
    if version.startswith("v") and version[1:].isdigit():
        return f"v{int(version[1:]) + 1}"
    raise ValueError(f"Invalid credential version: {version}")


def payload_expires_at(payload: CredentialPayload) -> datetime | None:
    value = payload.get("expires_at")
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    return None