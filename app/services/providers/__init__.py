"""Provider-agnostic LLM interface (issue #2).

Import from this package (or the compatibility façade ``app.services.llm``) to
obtain a provider::

    from app.services.providers import get_provider
    provider = get_provider()               # resolves LLM_PROVIDER
    response = provider.chat([{"role": "user", "content": "hi"}])

Built-in providers register themselves here on import. Adding another is a
three-step process documented in :mod:`app.services.providers.base`.
"""

from app.services.providers.anthropic import AnthropicProvider
from app.services.providers.base import (
    LLMProvider,
    ProviderAuthenticationError,
    ProviderConfigurationError,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponse,
    ProviderResponseError,
    ProviderUnavailableError,
    UnknownProviderError,
)
from app.services.providers.mock import MockProvider
from app.services.providers.openai import OpenAIProvider
from app.services.providers.registry import (
    available_providers,
    get_provider,
    register_provider,
    resolve_provider_name,
    unregister_provider,
)

register_provider(OpenAIProvider.name, OpenAIProvider)
register_provider(AnthropicProvider.name, AnthropicProvider)
register_provider(MockProvider.name, MockProvider)

__all__ = [
    "AnthropicProvider",
    "LLMProvider",
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
    "resolve_provider_name",
    "unregister_provider",
]
