# LLM provider layer (issue #2)

The assistant talks to LLM vendors through a single provider-agnostic interface.
Routes and services resolve a provider and call `chat()` / `stream()`; they never
touch a vendor SDK, HTTP status code, or response shape.

## Package layout

| Concern | Location |
| --- | --- |
| Abstract contract, `ProviderResponse`, error hierarchy | `app/services/providers/base.py` |
| Name → factory registry and `get_provider()` | `app/services/providers/registry.py` |
| OpenAI-compatible provider | `app/services/providers/openai.py` |
| Anthropic Messages API provider | `app/services/providers/anthropic.py` |
| Deterministic offline provider | `app/services/providers/mock.py` |
| Backward-compatible façade | `app/services/llm.py` |

## Selecting a provider

Set `LLM_PROVIDER` to a registered name (`mock` — default, `openai`,
`anthropic`). Explicit selection is possible with `get_provider("openai")`.

```python
from app.services.providers import get_provider

provider = get_provider()  # honours LLM_PROVIDER
response = provider.chat([{"role": "user", "content": "Explain this function."}])
print(response.content, response.total_tokens, response.latency_seconds)
```

Unknown names raise `UnknownProviderError` (a `ProviderConfigurationError`),
listing the available providers instead of silently falling back.

## Contract

```python
class LLMProvider(ABC):
    name: str
    models: tuple[str, ...]

    def chat(messages, *, model=None, params=None) -> ProviderResponse: ...
    def stream(messages, *, model=None, params=None) -> Iterator[str]: ...
    def complete(messages, *, stream=False) -> str: ...  # compatibility helper
```

`ProviderResponse` carries `content`, `model`, `prompt_tokens`,
`completion_tokens`, `total_tokens`, and `latency_seconds`. Failures use the
`ProviderError` hierarchy: `ProviderConfigurationError`,
`UnknownProviderError`, `ProviderAuthenticationError`, `ProviderRateLimitError`,
`ProviderUnavailableError`, and `ProviderResponseError`. `LLMProviderError` (the
historical name) is an alias of `ProviderError`, so existing `except
LLMProviderError` blocks keep working.

## Adding a provider

1. Create `app/services/providers/<name>.py` with a class that subclasses
   `LLMProvider`, sets `name` and `models`, and implements `chat()`/`stream()`.
   Do all network work here and normalize every failure to a `ProviderError`.
2. Register it in `app/services/providers/__init__.py`:
   `register_provider(YourProvider.name, YourProvider)`.
3. Configure and select it with `LLM_PROVIDER=<name>` and its own environment
   variables (documented in `.env.example`).
4. Add it to `tests/test_providers.py`; the shared contract test runs the same
   checks for every provider.

Providers must treat prompts as data, never log secrets, and avoid assuming a
specific response shape outside their own module.
