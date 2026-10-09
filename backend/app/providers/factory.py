from __future__ import annotations

import os

from .ai import AIProviderInterface
from .mock_ai import MockAIProvider

#: Adapters that ship in the box.  Anything else is a configuration error and
#: is reported as such rather than silently degrading to the mock provider,
#: which would hide the misconfiguration behind plausible-looking copy.
BUILTIN_PROVIDERS = frozenset({"mock", "mock_ai"})

#: Spellings accepted for the built-in deterministic provider.
_MOCK_ALIASES = frozenset({"", "mock", "mock_ai"})


class AIProviderUnavailable(RuntimeError):
    """The configured AI provider has no adapter in this build.

    Raised at request time by the services that need an assistant.  The API
    maps it to ``503`` so an operator sees an actionable message instead of an
    opaque ``500`` from an unhandled ``ValueError``.
    """

    def __init__(self, provider_name: str) -> None:
        self.provider_name = provider_name
        super().__init__(
            f"AI provider '{provider_name}' is not available: this build ships no adapter "
            f"for it. Registered adapters: {', '.join(sorted(BUILTIN_PROVIDERS))}. "
            "Set AI_PROVIDER to one of them."
        )


def build_ai_provider(provider_name: str | None = None) -> AIProviderInterface:
    """Resolve the configured provider through an adapter registry.

    Business logic never instantiates a concrete provider directly; it accepts
    an AIProviderInterface and this factory decides which adapter to use.
    """
    name = (provider_name or os.getenv("AI_PROVIDER") or "mock").lower()
    if name in _MOCK_ALIASES:
        return MockAIProvider()
    raise AIProviderUnavailable(name)
