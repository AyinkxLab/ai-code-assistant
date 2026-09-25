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
| Retry/backoff decorator | `app/services/providers/retry.py` |
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
    supported_models: tuple[str, ...]  # alias of models, used by the selector

    def chat(messages, *, model=None, params=None) -> ProviderResponse: ...
    def stream(messages, *, model=None, params=None) -> Iterator[str]: ...
    def complete(messages, *, stream=False) -> str: ...  # compatibility helper
```

Per-conversation generation settings (issue #12) resolve through
`app/services/provider_config.py`: `provider_options(user)` lists each provider
with its `supported_models` and whether the user has a usable key, and
`build_provider(user, name)` applies the user's stored (decrypted) key when the
environment does not provide one. A conversation's `model`, `temperature`, and
`system_prompt` are passed through to `chat()`/`stream()` on every message.

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

## Retries and backoff (issue #29)

`get_retrying_provider()` returns the resolved provider wrapped in
`RetryingProvider`, which retries transient failures — network errors, HTTP 429
and 5xx (`ProviderRateLimitError` / `ProviderUnavailableError`) — with
exponential backoff plus jitter. Non-transient failures (`401`/`400`,
configuration, unexpected response shape) are re-raised immediately so they
fail fast.

```python
from app.services.providers import get_retrying_provider

provider = get_retrying_provider()
response = provider.chat([{"role": "user", "content": "hi"}])
```

Retry behaviour is configurable via the environment and bounded by default:

| Variable | Default | Meaning |
| --- | --- | --- |
| `LLM_MAX_RETRIES` | `3` | Retries after the initial attempt |
| `LLM_RETRY_BASE_DELAY` | `0.5` | Base delay in seconds |
| `LLM_RETRY_MAX_DELAY` | `8.0` | Cap on any single delay |

The delay before retry `n` is `LLM_RETRY_BASE_DELAY * 2 ** (n - 1)`, capped at
`LLM_RETRY_MAX_DELAY`, then multiplied by a random jitter in `[0, 1]`. Streaming
retries only while establishing the stream: after the first chunk is emitted a
partial reply is never replayed. When every retry is exhausted the chat stream
endpoint emits an SSE `{"type": "error"}` event. Use `is_transient_error(exc)`
to test whether an error is retryable.
