"""Backward-compatible façade over :mod:`app.services.providers` (issue #2).

The provider abstraction now lives in ``app/services/providers/``. This module
keeps the historical import surface — ``get_provider``, ``LLMProviderError``,
``OpenAIProvider``, ``MockProvider`` — so existing call sites and tests continue
to work unchanged. ``LLMProviderError`` is an alias of
:class:`~app.services.providers.base.ProviderError`, so catching it also catches
the specific subclasses (configuration, auth, rate limit, unavailable, response).

This module also exposes the streaming façade used by the SSE endpoint:
:func:`stream_provider` and the associated :class:`StreamEvent` /
:class:`StreamEventType` types. It delegates to the provider's optional
``stream()`` method when available and falls back to a single-shot ``complete()``
call otherwise, so every provider can participate in the streaming protocol.
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from enum import StrEnum

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

logger = logging.getLogger(__name__)


class StreamEventType(StrEnum):
    """Event types emitted by :func:`stream_provider`."""

    MESSAGE_START = "message_start"
    CONTENT = "content"
    MESSAGE_END = "message_end"
    ERROR = "error"


@dataclass(slots=True)
class StreamEvent:
    """A single event in the provider stream protocol.

    The ``data`` payload is JSON-serializable and matches the SSE event body
    expected by the browser client.
    """

    type: StreamEventType
    data: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"type": self.type.value, "data": self.data}


def _normalize_usage(usage: dict | None) -> dict:
    """Return a stable token-usage dict for the ``message_end`` event."""
    if not usage:
        return {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    prompt = int(usage.get("prompt_tokens", 0) or 0)
    completion = int(usage.get("completion_tokens", 0) or 0)
    total = int(usage.get("total_tokens", prompt + completion) or 0)
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


def _extract_delta(chunk: object) -> str:
    """Pull a text delta out of a provider stream chunk.

    Supports the common shapes used by OpenAI-compatible and Anthropic
    streaming responses, as well as plain strings and dicts with a ``text``
    or ``content`` key.
    """
    if chunk is None:
        return ""
    if isinstance(chunk, str):
        return chunk
    if isinstance(chunk, dict):
        for key in ("text", "content", "delta"):
            value = chunk.get(key)
            if isinstance(value, str):
                return value
            if isinstance(value, dict):
                inner = value.get("content")
                if isinstance(inner, str):
                    return inner
            if isinstance(value, list):
                parts = []
                for item in value:
                    if isinstance(item, str):
                        parts.append(item)
                    elif isinstance(item, dict) and isinstance(item.get("text"), str):
                        parts.append(item["text"])
                if parts:
                    return "".join(parts)
        choices = chunk.get("choices")
        if isinstance(choices, list) and choices:
            return _extract_delta(choices[0])
        return ""
    # Objects with attributes (e.g. provider SDK chunk types).
    for attr in ("text", "content", "delta"):
        value = getattr(chunk, attr, None)
        if isinstance(value, str):
            return value
    choices = getattr(chunk, "choices", None)
    if isinstance(choices, list) and choices:
        return _extract_delta(choices[0])
    return ""


def _extract_usage(chunk: object) -> dict | None:
    """Pull a token-usage dict out of a provider stream chunk, if present."""
    if isinstance(chunk, dict):
        usage = chunk.get("usage")
        if isinstance(usage, dict):
            return usage
        choices = chunk.get("choices")
        if isinstance(choices, list) and choices:
            return _extract_usage(choices[0])
        return None
    usage = getattr(chunk, "usage", None)
    if isinstance(usage, dict):
        return usage
    choices = getattr(chunk, "choices", None)
    if isinstance(choices, list) and choices:
        return _extract_usage(choices[0])
    return None


async def _iterate_provider_stream(
    provider: LLMProvider,
    messages: list,
    model: str | None,
    **kwargs,
) -> AsyncIterator[object]:
    """Iterate a provider's native stream if it has one.

    Synchronous generators (the common case for the HTTP-based providers) are
    forwarded chunk by chunk; an async provider iterator is awaited in turn.
    """
    stream_fn = getattr(provider, "stream", None)
    if callable(stream_fn):
        iterator = stream_fn(messages, model=model, **kwargs)
        if hasattr(iterator, "__aiter__"):
            async for chunk in iterator:
                yield chunk
            return
        for chunk in iterator:
            yield chunk
        return

    # Fallback: providers without native streaming still participate in the
    # protocol by emitting their completion as a single chunk.
    response = await provider.complete(messages, model=model, **kwargs)
    yield {
        "text": getattr(response, "content", "") or "",
        "usage": getattr(response, "usage", None),
    }


async def stream_provider(
    provider: LLMProvider | str | None = None,
    messages: list | None = None,
    model: str | None = None,
    **kwargs,
) -> AsyncIterator[StreamEvent]:
    """Stream a chat completion as :class:`StreamEvent` objects.

    Yields ``message_start``, one or more ``content`` deltas, then
    ``message_end`` with token usage. Any error raised by the provider is
    surfaced as an ``error`` event so the SSE endpoint never hangs.

    The generator is cancellation-aware. If the consumer closes it (e.g. on
    client disconnect), the underlying provider stream is closed and the
    partial content is emitted through a final ``content`` event before the
    generator exits, allowing the caller to persist what was received.
    """
    if messages is None:
        messages = []
    if isinstance(provider, str) or provider is None:
        provider = get_provider(provider)

    content_parts: list[str] = []
    usage: dict | None = None
    cancelled = False

    yield StreamEvent(
        StreamEventType.MESSAGE_START,
        {"model": model or getattr(provider, "model", None)},
    )

    try:
        async for chunk in _iterate_provider_stream(provider, messages, model, **kwargs):
            delta = _extract_delta(chunk)
            chunk_usage = _extract_usage(chunk)
            if chunk_usage:
                usage = chunk_usage
            if delta:
                content_parts.append(delta)
                yield StreamEvent(StreamEventType.CONTENT, {"delta": delta})
    except GeneratorExit:
        # Client disconnected: stop the provider stream and surface the
        # partial content so the caller can persist it.
        cancelled = True
        partial = "".join(content_parts)
        logger.info("Provider stream cancelled by consumer")
        yield StreamEvent(
            StreamEventType.CONTENT,
            {"delta": "", "cancelled": True, "partial": partial},
        )
        yield StreamEvent(
            StreamEventType.MESSAGE_END,
            {
                "content": partial,
                "usage": _normalize_usage(usage),
                "cancelled": True,
            },
        )
        return
    except Exception as exc:
        logger.exception("Provider stream failed")
        yield StreamEvent(
            StreamEventType.ERROR,
            {
                "message": str(exc) or exc.__class__.__name__,
                "type": exc.__class__.__name__,
                "partial": "".join(content_parts),
            },
        )
        return

    final_content = "".join(content_parts)
    yield StreamEvent(
        StreamEventType.MESSAGE_END,
        {
            "content": final_content,
            "usage": _normalize_usage(usage),
            "cancelled": cancelled,
        },
    )


def stream_provider_sync(
    provider: LLMProvider | str | None = None,
    messages: list | None = None,
    model: str | None = None,
    **kwargs,
) -> AsyncIterator[StreamEvent]:
    """Alias for :func:`stream_provider` kept for naming compatibility."""
    return stream_provider(provider, messages, model, **kwargs)


def is_streaming_supported(provider: LLMProvider | str | None = None) -> bool:
    """Report whether the given provider has native streaming support."""
    if provider is None or isinstance(provider, str):
        try:
            provider = get_provider(provider)
        except Exception:
            return False
    return callable(getattr(provider, "stream", None))


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
    "StreamEvent",
    "StreamEventType",
    "UnknownProviderError",
    "available_providers",
    "get_provider",
    "get_retrying_provider",
    "is_streaming_supported",
    "is_transient_error",
    "provider_status",
    "register_provider",
    "stream_provider",
    "stream_provider_sync",
]
