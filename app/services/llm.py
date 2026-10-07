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
    FailoverProvider,
    LLMProvider,
    MockProvider,
    OpenAIProvider,
    ProviderAttempt,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderError,
    ProviderFailoverError,
    ProviderRateLimitError,
    ProviderResponse,
    ProviderResponseError,
    ProviderUnavailableError,
    RetryingProvider,
    UnknownProviderError,
    available_providers,
    build_failover_chain,
    classify_error,
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
    "FailoverProvider",
    "LLMProvider",
    "LLMProviderError",
    "MockProvider",
    "OpenAIProvider",
    "ProviderAttempt",
    "ProviderAuthenticationError",
    "ProviderConfigurationError",
    "ProviderError",
    "ProviderFailoverError",
    "ProviderRateLimitError",
    "ProviderResponse",
    "ProviderResponseError",
    "ProviderUnavailableError",
    "RetryingProvider",
    "UnknownProviderError",
    "available_providers",
    "build_failover_chain",
    "classify_error",
    "get_provider",
    "get_retrying_provider",
    "is_transient_error",
    "provider_status",
    "register_provider",
]
