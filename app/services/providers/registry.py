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


def _stored_api_key(user, provider_name: str) -> str | None:
    """Return the user's active stored key for ``provider_name``, or ``None``.

    Imports are lazy so the registry stays free of model/app imports at module
    load time (avoids circular imports during app startup).
    """
    if user is None:
        return None
    try:
        from app.models import ApiKey
        from app.services.api_keys import decrypt_for_use
    except Exception:  # pragma: no cover - defensive
        return None
    key = (
        ApiKey.query.filter_by(
            user_id=getattr(user, "id", None), provider=provider_name, is_active=True
        )
        .order_by(ApiKey.created_at.desc())
        .first()
    )
    if key is None:
        return None
    try:
        return decrypt_for_use(key)
    except Exception:  # pragma: no cover - defensive
        return None


def get_provider(name: str | None = None, *, user=None) -> LLMProvider:
    """Instantiate the provider selected by ``name`` (or ``LLM_PROVIDER``).

    When ``user`` is supplied and the provider requires a key that is absent
    from the environment, the user's active stored key for that provider is
    injected (issue #25), so a key added through the API-key UI unlocks chat
    without a restart.

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
    provider = factory()
    if getattr(provider, "requires_key", False) and not getattr(provider, "api_key", ""):
        stored = _stored_api_key(user, resolved)
        if stored:
            try:
                provider = factory(api_key=stored)
            except TypeError:
                provider.api_key = stored
    return provider


def provider_status(user=None, name: str | None = None) -> dict:
    """Report whether the selected provider is ready to serve requests.

    Returns a small dict the UI can act on::

        {"provider": "openai", "requires_key": True,
         "configured": False, "reason": "missing_key"}

    ``reason`` is ``None`` (keyless provider or env key present),
    ``"environment"``, ``"stored_key"``, ``"missing_key"`` or
    ``"unknown_provider"``.
    """
    resolved = resolve_provider_name(name)
    factory = _FACTORIES.get(resolved)
    if factory is None:
        return {
            "provider": resolved,
            "requires_key": True,
            "configured": False,
            "reason": "unknown_provider",
        }
    probe = factory()
    if not getattr(probe, "requires_key", False):
        return {"provider": resolved, "requires_key": False, "configured": True, "reason": None}
    if getattr(probe, "api_key", ""):
        return {
            "provider": resolved,
            "requires_key": True,
            "configured": True,
            "reason": "environment",
        }
    if _stored_api_key(user, resolved):
        return {
            "provider": resolved,
            "requires_key": True,
            "configured": True,
            "reason": "stored_key",
        }
    return {
        "provider": resolved,
        "requires_key": True,
        "configured": False,
        "reason": "missing_key",
    }
