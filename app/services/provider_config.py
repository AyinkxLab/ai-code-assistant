"""Provider availability, per-conversation model/provider resolution (#12).

The model selector needs to know which providers the current user can actually
use (an active stored key or an environment key) and which models each one
supports. :func:`provider_options` builds that list; :func:`build_provider`
constructs a provider with the user's stored key applied so a conversation's
chosen provider/model is what actually runs.

All validation errors are :class:`ProviderSettingsError`, which the chat routes
translate into a 400 response.
"""

from __future__ import annotations

from typing import Any

from app.extensions import db
from app.models import ApiKey, Conversation
from app.services.api_keys import decrypt_for_use
from app.services.providers.base import LLMProvider, ProviderError
from app.services.providers.registry import available_providers, get_provider

#: Upper bound for a conversation's system-prompt override.
MAX_SYSTEM_PROMPT_CHARS = 4000

#: Temperature range accepted from the UI (OpenAI's documented range).
TEMPERATURE_MIN = 0.0
TEMPERATURE_MAX = 2.0

#: Temperature used by the UI when a conversation has no explicit value.
DEFAULT_TEMPERATURE = 0.7


class ProviderSettingsError(ValueError):
    """Raised when requested generation settings are invalid or unavailable."""


def provider_options(user) -> list[dict[str, Any]]:
    """Return every registered provider with its models and availability.

    A provider is ``available`` when it needs no key (the mock provider), has
    an environment key, or the user has stored an active key for it. The
    selector greys out unavailable providers and links to the keys page.
    """
    user_key_names = {
        key.provider for key in ApiKey.query.filter_by(user_id=user.id, is_active=True).all()
    }
    options: list[dict[str, Any]] = []
    for name in available_providers():
        try:
            provider = get_provider(name)
        except ProviderError:  # pragma: no cover - registry is internally consistent
            continue
        models = list(provider.supported_models)
        if not models:
            continue
        requires_key = name != "mock"
        env_key = bool(getattr(provider, "api_key", ""))
        has_key = (not requires_key) or env_key or name in user_key_names
        options.append(
            {
                "name": name,
                "models": models,
                "requires_key": requires_key,
                "has_key": has_key,
                "available": has_key,
            }
        )
    return options


def build_provider(user, name: str | None = None) -> LLMProvider:
    """Construct the selected provider, applying the user's stored key if needed.

    The provider's own environment key wins when present; otherwise an active
    key stored by ``user`` for that provider is decrypted in memory and used
    for this call only. Raises :class:`~app.services.providers.base.ProviderError`
    for an unknown provider.
    """
    provider = get_provider(name)
    if provider.name == "mock" or getattr(provider, "api_key", ""):
        return provider
    key = (
        ApiKey.query.filter_by(user_id=user.id, provider=provider.name, is_active=True)
        .order_by(ApiKey.created_at.desc())
        .first()
    )
    if key is not None and hasattr(provider, "api_key"):
        provider.api_key = decrypt_for_use(key)
    return provider


def _provider_models(name: str | None) -> set[str]:
    names = [name] if name else available_providers()
    models: set[str] = set()
    for provider_name in names:
        try:
            models.update(get_provider(provider_name).supported_models)
        except ProviderError:
            continue
    return models


def apply_settings(conversation: Conversation, data: dict, user) -> None:
    """Validate and apply generation settings from ``data`` to ``conversation``.

    Only keys present in ``data`` are changed. Raises
    :class:`ProviderSettingsError` for an unavailable provider, an unsupported
    model, an out-of-range temperature, or an oversized system prompt.
    """
    if "provider" in data:
        raw = data.get("provider")
        if raw in (None, ""):
            conversation.provider = None
        else:
            provider_name = str(raw).strip().lower()[:50]
            available = {option["name"] for option in provider_options(user) if option["available"]}
            if provider_name not in available:
                raise ProviderSettingsError(
                    f"The '{provider_name}' provider is not available. "
                    "Add an API key on the Keys page first."
                )
            conversation.provider = provider_name

    if "model" in data:
        raw = data.get("model")
        if raw in (None, ""):
            conversation.model = None
        else:
            model = str(raw).strip()[:100]
            allowed = _provider_models(conversation.provider)
            if allowed and model not in allowed:
                raise ProviderSettingsError(f"'{model}' is not a supported model.")
            conversation.model = model

    if "temperature" in data:
        raw = data.get("temperature")
        if raw in (None, ""):
            conversation.temperature = None
        else:
            try:
                temperature = float(raw)
            except (TypeError, ValueError):
                raise ProviderSettingsError("Temperature must be a number.") from None
            if not (TEMPERATURE_MIN <= temperature <= TEMPERATURE_MAX):
                raise ProviderSettingsError(
                    f"Temperature must be between {TEMPERATURE_MIN} and {TEMPERATURE_MAX}."
                )
            conversation.temperature = temperature

    if "system_prompt" in data:
        raw = data.get("system_prompt")
        if raw in (None, ""):
            conversation.system_prompt = None
        else:
            prompt = str(raw).strip()
            if len(prompt) > MAX_SYSTEM_PROMPT_CHARS:
                raise ProviderSettingsError(
                    f"System prompt must be {MAX_SYSTEM_PROMPT_CHARS} characters or fewer."
                )
            conversation.system_prompt = prompt or None


def save_settings(conversation: Conversation, data: dict, user) -> Conversation:
    """Apply ``data`` and commit. Raises :class:`ProviderSettingsError`."""
    apply_settings(conversation, data, user)
    db.session.commit()
    return conversation
