"""Backward-compatible facade over :mod:`app.services.providers` (issue #2).

The provider abstraction now lives in ``app/services/providers/``. This module
keeps the historical import surface — ``get_provider``, ``LLMProviderError``,
``OpenAIProvider``, ``MockProvider`` in the module namespace, and re-exports the
retry / error-classification helpers added for issue #2.

``LLMProviderError`` remains an alias of
``services.providers.base.ProviderError``, so catching it also catches
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
    RetryingProvider,
    UnknownProviderError,
    available_providers,
    get_provider,
    get_retrying_provider,
    is_transient_error,
    provider_status,
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
    "RetryingProvider",
    "UnknownProviderError",
    "available_providers",
    "get_provider",
    "get_retrying_provider",
    "is_transient_error",
    "provider_status",
    "register_provider",
]
