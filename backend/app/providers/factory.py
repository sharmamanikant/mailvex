from __future__ import annotations

import os

from .ai import AIProviderInterface
from .mock_ai import MockAIProvider


def build_ai_provider(provider_name: str | None = None) -> AIProviderInterface:
    """Resolve the configured provider through an adapter registry.

    Business logic never instantiates a concrete provider directly; it accepts
    an AIProviderInterface and this factory decides which adapter to use.
    """
    name = (provider_name or os.getenv("AI_PROVIDER") or "mock").lower()
    if name in {"", "mock", "mock_ai"}:
        return MockAIProvider()
    raise ValueError(f"AI provider '{name}' is not configured")