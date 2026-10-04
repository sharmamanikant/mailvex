"""Email provider registry (System B foundation).

Providers register statically; the registry returns singleton instances
backed by architecture-only stubs. Real provider modules slot into the same
registry in a later phase without touching API/service layer code.
"""

from __future__ import annotations

from app.email_providers.base import EmailProviderBase, ProviderCapabilities
from app.email_providers.google.provider import GoogleEmailProvider
from app.email_providers.microsoft.provider import MicrosoftEmailProvider
from app.email_providers.sendgrid.provider import SendGridEmailProvider
from app.email_providers.smtp.provider import SMTPEmailProvider
from app.email_providers.zoho.provider import ZohoEmailProvider

__all__ = ["ProviderRegistry", "get_provider", "list_providers"]

_PROVIDER_CLASSES: tuple[type[EmailProviderBase], ...] = (
    GoogleEmailProvider,
    MicrosoftEmailProvider,
    ZohoEmailProvider,
    SendGridEmailProvider,
    SMTPEmailProvider,
)


class ProviderRegistry:
    def __init__(self) -> None:
        self._instances: dict[str, EmailProviderBase] = {}
        for cls in _PROVIDER_CLASSES:
            instance = cls()
            self._instances[instance.get_provider_name().upper()] = instance

    def get(self, name: str) -> EmailProviderBase:
        normalized = name.upper()
        try:
            return self._instances[normalized]
        except KeyError:
            raise KeyError(f"Unknown email provider: {name}") from None

    def capabilities(self) -> list[ProviderCapabilities]:
        return [instance.get_capabilities() for instance in self._instances.values()]

    def names(self) -> list[str]:
        return [instance.get_provider_name() for instance in self._instances.values()]


_registry = ProviderRegistry()


def get_provider(name: str) -> EmailProviderBase:
    return _registry.get(name)


def list_providers() -> list[ProviderCapabilities]:
    return _registry.capabilities()