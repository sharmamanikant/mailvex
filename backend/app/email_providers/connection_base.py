"""Provider-connection abstraction (Phase 1 provider foundation).

An organization-level connection is distinct from an individual sender
mailbox. Providers that support the *connection* flow (e.g. a Google
Workspace authorization) implement :class:`EmailProviderConnection` so Phase 6/7
providers (Microsoft, SendGrid, Zoho, SMTP) can be added without restructuring
the ``ProviderConnection`` model, its service layer or its API.

The interface mirrors the orchestration the service layer performs:

* ``get_authorization_url`` -> build the server-controlled OAuth start URL.
* ``handle_callback`` -> exchange the code, validate the returned identity and
  return a normalized :class:`ProviderIdentity` plus the credential payload
  that the service layer encrypts before persistence. Raw secrets never cross
  the API boundary.
* ``refresh_credentials`` -> rotate the persisted (encrypted) reference.
* ``disconnect`` -> provider-side cleanup; the service layer tombstones the
  stored reference and marks the connection ``DISCONNECTED``.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, ClassVar

from app.email_providers.base import CredentialRotationResult

__all__ = [
    "AUTH_REQUIRED",
    "EMAIL_PROVIDER_CONNECTION",
    "IDENTITY_FAILED",
    "INSUFFICIENT_SCOPE",
    "NOT_CONFIGURED",
    "OAUTH_EXCHANGE_FAILED",
    "REFRESH_FAILED",
    "STATE_EXPIRED",
    "STATE_INVALID",
    "EmailProviderConnection",
    "OAuthCallbackResult",
    "ProviderConnectionError",
    "ProviderIdentity",
    "get_connection_provider",
]

SCOPE_CANCELLED = "SCOPE_CANCELLED"
INSUFFICIENT_SCOPE = "INSUFFICIENT_SCOPE"
IDENTITY_FAILED = "IDENTITY_FAILED"
OAUTH_EXCHANGE_FAILED = "OAUTH_EXCHANGE_FAILED"
NOT_CONFIGURED = "NOT_CONFIGURED"
AUTH_REQUIRED = "AUTH_REQUIRED"
REFRESH_FAILED = "REFRESH_FAILED"
STATE_INVALID = "STATE_INVALID"
STATE_EXPIRED = "STATE_EXPIRED"

# Sentinel marking an ``email_provider_connection`` resource type in audit logs.
EMAIL_PROVIDER_CONNECTION = "provider_connection"


class ProviderConnectionError(ValueError):
    """Raised for safe, user-facing connection failures.

    ``code`` maps to a stable, non-secret error token (see the module-level
    ``*_CODE`` constants). Never carry secrets inside the message.
    """

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ProviderIdentity:
    """The verified provider account/workspace identity captured from OAuth.

    ``workspace_domain`` is the hosted-domain claim (or the account's email
    domain). Phase 2 validates it against the workspace directory before any
    mailbox is promoted to a Sender.
    """

    provider: str
    provider_account_id: str
    email: str
    workspace_domain: str | None
    display_name: str | None
    scopes: tuple[str, ...] = field(default_factory=tuple)


@dataclass(frozen=True)
class OAuthCallbackResult:
    """Outcome of an OAuth callback: identity + the secrets to encrypt.

    The ``credential_payload`` contains raw tokens and is consumed inside the
    service boundary ONLY (immediately encrypted). It is never returned to an
    API consumer, written to audit records, or logged.
    """

    identity: ProviderIdentity
    credential_payload: dict[str, Any] = field(default_factory=dict)
    # Provider-authored, non-secret connection metadata persisted by the
    # service layer (e.g. ``microsoftTenantId`` / ``organizationName``). Raw
    # tokens must NEVER live here — credentials belong in ``credential_payload``
    # only and are encrypted immediately by the service.
    connection_metadata: dict[str, Any] = field(default_factory=dict)


class EmailProviderConnection(ABC):
    """Connection-flow interface implemented by provider adapters."""

    provider_name: ClassVar[str] = ""

    @abstractmethod
    def get_authorization_url(self, *, state: str) -> str:
        """Return the server-controlled OAuth authorization URL."""

    @abstractmethod
    def handle_callback(
        self,
        *,
        code: str,
        state: str,
        expected_scopes: frozenset[str],
    ) -> OAuthCallbackResult:
        """Exchange the authorization code and return the validated identity."""

    @abstractmethod
    def refresh_credentials(
        self,
        *,
        credential_reference: str,
        credential_version: str,
    ) -> CredentialRotationResult:
        """Rotate the persisted (encrypted) credential reference."""

    @abstractmethod
    def disconnect(self) -> None:
        """Best-effort provider-side cleanup (revoke/forget server state)."""

    def revoke_token(self, refresh_token: str) -> None:
        """Best-effort server-side token revocation. Default: no-op."""
        return None


_CONNECTION_PROVIDERS: dict[str, type[EmailProviderConnection]] = {}


def _register_connection_provider(cls: type[EmailProviderConnection]) -> type[EmailProviderConnection]:
    name = getattr(cls, "provider_name", "").upper()
    if name:
        _CONNECTION_PROVIDERS[name] = cls
    return cls


def get_connection_provider(name: str) -> EmailProviderConnection:
    """Return the singleton connection-flow adapter for ``name``."""
    normalized = name.upper()
    try:
        provider_cls = _CONNECTION_PROVIDERS[normalized]
    except KeyError:
        raise ProviderConnectionError(
            "UNSUPPORTED_PROVIDER",
            f"The {name} provider connection flow is not implemented yet",
        ) from None
    return provider_cls()


def _register_all() -> None:
    from app.email_providers.google.workspace import GoogleWorkspaceProviderConnection
    from app.email_providers.microsoft.discovery import (
        _register_microsoft_discovery_provider,
    )
    from app.email_providers.microsoft.workspace import (
        MicrosoftWorkspaceProviderConnection,
    )

    _register_connection_provider(GoogleWorkspaceProviderConnection)
    _register_connection_provider(MicrosoftWorkspaceProviderConnection)
    _register_microsoft_discovery_provider()


_register_all()