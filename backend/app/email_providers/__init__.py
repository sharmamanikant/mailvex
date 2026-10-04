"""System B email-provider boundary.

Public surface: base interfaces, the provider registry, and the secure
credential lifecycle. Google (Gmail API), Microsoft 365 (Graph), Zoho and
generic SMTP implement real OAuth/transport sending; SendGrid uses an API key.
Inbox sync, delivery-event sync and webhooks remain explicitly unimplemented
(base raises :class:`ProviderMethodNotImplemented`) until those phases land.
"""

from app.email_providers.base import (
    Attachment,
    CredentialRotationResult,
    DeliveryEventCursor,
    DiscoveryResult,
    EmailMessage,
    EmailProviderBase,
    EmailProviderError,
    ProviderCapabilities,
    ProviderConnectionConfig,
    ProviderConnectionRequirementError,
    ProviderErrorCode,
    ProviderMethodNotImplemented,
    ProviderSenderProfile,
    ValidationResult,
    WebhookEvent,
)
from app.email_providers.connection_base import (
    AUTH_REQUIRED,
    IDENTITY_FAILED,
    INSUFFICIENT_SCOPE,
    NOT_CONFIGURED,
    OAUTH_EXCHANGE_FAILED,
    STATE_EXPIRED,
    STATE_INVALID,
    EmailProviderConnection,
    OAuthCallbackResult,
    ProviderConnectionError,
    ProviderIdentity,
    get_connection_provider,
)
from app.email_providers.credentials import (
    CredentialPayload,
    decrypt_credential_reference,
    encrypt_credential_reference,
    invalidate_credential_reference,
    rotate_credential_reference,
)
from app.email_providers.registry import get_provider, list_providers

__all__ = [
    "AUTH_REQUIRED",
    "IDENTITY_FAILED",
    "INSUFFICIENT_SCOPE",
    "NOT_CONFIGURED",
    "OAUTH_EXCHANGE_FAILED",
    "STATE_EXPIRED",
    "STATE_INVALID",
    "Attachment",
    "CredentialPayload",
    "CredentialRotationResult",
    "DeliveryEventCursor",
    "DiscoveryResult",
    "EmailMessage",
    "EmailProviderBase",
    "EmailProviderConnection",
    "EmailProviderError",
    "OAuthCallbackResult",
    "ProviderCapabilities",
    "ProviderConnectionConfig",
    "ProviderConnectionError",
    "ProviderConnectionRequirementError",
    "ProviderErrorCode",
    "ProviderIdentity",
    "ProviderMethodNotImplemented",
    "ProviderSenderProfile",
    "ValidationResult",
    "WebhookEvent",
    "decrypt_credential_reference",
    "encrypt_credential_reference",
    "get_connection_provider",
    "get_provider",
    "invalidate_credential_reference",
    "list_providers",
    "rotate_credential_reference",
]