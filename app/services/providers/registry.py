"""Provider registry: register factories by name and resolve by config (#2).

Providers register themselves on import (see ``app/services/providers/__init__``)
and are resolved through ``LLM_PROVIDER`` or an explicit name. Unknown names
raise :class:`UnknownProviderError` with the list of available providers, so the
failure is clear and user-facing rather than a silent fallback.
"""

from __future__ import annotations

import os
from collections.abc import Callable

from app.services.providers.base import LLMProvider, UnknownProviderError

_FACTORIES: dict[str, Callable[[], LLMProvider]] = {}


def register_provider(name: str, factory: Callable[[], LLMProvider]) -> None:
    """Register ``factory`` under ``name`` (case-insensitive)."""
    _FACTORIES[(name or "").strip().lower()] = factory


def unregister_provider(name: str) -> None:
    """Remove a provider registration (primarily for tests)."""
    _FACTORIES.pop((name or "").strip().lower(), None)


def available_providers() -> list[str]:
    """Return the sorted names of every registered provider."""
    return sorted(_FACTORIES)


def resolve_provider_name(name: str | None = None) -> str:
    """Resolve the provider name from an argument or the ``LLM_PROVIDER`` env."""
    return (name or os.getenv("LLM_PROVIDER") or "mock").strip().lower()


def get_provider(name: str | None = None) -> LLMProvider:
    """Instantiate the provider selected by ``name`` (or ``LLM_PROVIDER``).

    Raises :class:`UnknownProviderError` when the provider is not registered.
    """
    resolved = resolve_provider_name(name)
    factory = _FACTORIES.get(resolved)
    if factory is None:
        available = ", ".join(available_providers()) or "none"
        raise UnknownProviderError(
            f"Unknown LLM provider '{resolved}'. Available providers: {available}.",
            provider=resolved,
        )
    return factory()
