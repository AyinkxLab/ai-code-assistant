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


def _current_user_or_none():
    """Return the logged-in user when there is an active request context (#30)."""
    try:
        from flask_login import current_user
    except Exception:  # pragma: no cover - flask_login always present in the app
        return None
    try:
        if getattr(current_user, "is_authenticated", False):
            return current_user
    except Exception:  # no request context
        return None
    return None


def _stored_api_key(user, provider_name: str) -> str | None:
    """Decrypt the user's active stored key for ``provider_name``, or ``None``.

    Imports are lazy so the registry stays free of model/app imports at module
    load time (avoids circular imports during app startup). Decryption lives
    here — the provider service layer — and the plaintext never leaves it.
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
    except Exception:  # pragma: no cover - rotated secret / tampered row
        return None


def get_provider(name: str | None = None, *, user=None) -> LLMProvider:
    """Instantiate the provider selected by ``name`` (or ``LLM_PROVIDER``).

    When the provider needs an API key and the environment does not supply one,
    the signed-in user's stored (encrypted) key is decrypted and injected here,
    in the provider service layer (#30). ``user`` may be passed explicitly;
    otherwise the current request's logged-in user is used when available.

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
    resolved_user = user if user is not None else _current_user_or_none()
    if (
        resolved_user is not None
        and hasattr(provider, "api_key")
        and not getattr(provider, "api_key", "")
    ):
        stored = _stored_api_key(resolved_user, resolved)
        if stored:
            provider.api_key = stored
    return provider
