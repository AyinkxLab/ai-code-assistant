"""Backward-compatible façade over :mod:`app.services.providers` (issue #2).

The provider abstraction now lives in ``app/services/providers/``. This module
keeps the historical import surface — ``get_provider``, ``LLMProviderError``,
``OpenAIProvider``, ``MockProvider`` — so existing call sites and tests continue
to work unchanged. ``LLMProviderError`` is an alias of
:class:`~app.services.providers.base.ProviderError`, so catching it also catches
the specific subclasses (configuration, auth, rate limit, unavailable, response).
"""

from app.services.providers import (
    AnthropicProvider,
    LLMProvider,
    MockProvider,
    OpenAIProvider,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponse,
    ProviderResponseError,
    ProviderUnavailableError,
    UnknownProviderError,
    available_providers,
    get_provider,
    register_provider,
)

# Historical name used throughout the codebase.
LLMProviderError = ProviderError

__all__ = [
    "AnthropicProvider",
    "LLMProvider",
    "LLMProviderError",
    "MockProvider",
    "OpenAIProvider",
    "ProviderAuthenticationError",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderRateLimitError",
    "ProviderResponse",
    "ProviderResponseError",
    "ProviderUnavailableError",
    "UnknownProviderError",
    "available_providers",
    "get_provider",
    "register_provider",
]
