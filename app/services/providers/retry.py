"""Exponential-backoff retries for transient provider failures (issue #29).

:class:`RetryingProvider` decorates any
:class:`~app.services.providers.base.LLMProvider` so that transient failures —
network errors and HTTP 429/5xx, surfaced as ``ProviderUnavailableError`` /
``ProviderRateLimitError`` — are retried with exponential backoff plus jitter.
Non-transient failures (authentication, malformed request, unexpected response,
configuration) are re-raised immediately so callers fail fast instead of
retrying a request that cannot succeed.

Retries are bounded by ``LLM_MAX_RETRIES`` (default 3). The delay before the
``n``-th retry is ``LLM_RETRY_BASE_DELAY * 2 ** (n - 1)`` seconds, capped at
``LLM_RETRY_MAX_DELAY`` and multiplied by a random jitter in ``[0, 1]`` so that
many clients failing at once do not retry in lockstep.

Streaming is retried only while the stream is being established. Once the first
chunk has been yielded the attempt is considered successful: replaying a
partially-emitted stream would duplicate output for the user. A provider that
fails before producing any chunk is retried like ``chat()``.
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable, Iterable, Iterator
from typing import Any

from app.services.providers.base import (
    LLMProvider,
    ProviderError,
    ProviderRateLimitError,
    ProviderResponse,
    ProviderUnavailableError,
)

#: Provider errors that describe a transient condition worth retrying.
TRANSIENT_PROVIDER_ERRORS: tuple[type[ProviderError], ...] = (
    ProviderRateLimitError,
    ProviderUnavailableError,
)

DEFAULT_MAX_RETRIES = 3
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 8.0


def is_transient_error(exc: BaseException) -> bool:
    """Return ``True`` when ``exc`` is a provider error worth retrying."""
    return isinstance(exc, TRANSIENT_PROVIDER_ERRORS)


def _env_int(name: str, default: int) -> int:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0, int(raw))
    except ValueError:
        return default


def _env_float(name: str, default: float) -> float:
    raw = os.getenv(name)
    if raw is None or not raw.strip():
        return default
    try:
        return max(0.0, float(raw))
    except ValueError:
        return default


class RetryingProvider(LLMProvider):
    """Wrap a provider, retrying transient failures with backoff and jitter."""

    def __init__(
        self,
        provider: LLMProvider,
        *,
        max_retries: int | None = None,
        base_delay: float | None = None,
        max_delay: float | None = None,
        sleep: Callable[[float], None] = time.sleep,
        jitter: Callable[[], float] = random.random,
    ) -> None:
        self.provider = provider
        self.name = provider.name
        self.models = provider.models
        self.max_retries = _env_int("LLM_MAX_RETRIES", DEFAULT_MAX_RETRIES)
        if max_retries is not None:
            self.max_retries = max(0, max_retries)
        self.base_delay = _env_float("LLM_RETRY_BASE_DELAY", DEFAULT_BASE_DELAY)
        if base_delay is not None:
            self.base_delay = max(0.0, base_delay)
        self.max_delay = _env_float("LLM_RETRY_MAX_DELAY", DEFAULT_MAX_DELAY)
        if max_delay is not None:
            self.max_delay = max(0.0, max_delay)
        self._sleep = sleep
        self._jitter = jitter

    def retry_delay(self, attempt: int) -> float:
        """Delay before retry ``attempt`` (1-based), with full jitter."""
        exponential = self.base_delay * (2 ** (attempt - 1))
        capped = min(exponential, self.max_delay)
        return capped * self._jitter()

    def _wait(self, attempt: int) -> None:
        delay = self.retry_delay(attempt)
        if delay > 0:
            self._sleep(delay)

    def chat(
        self,
        messages: Iterable[Any],
        *,
        model: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> ProviderResponse:
        messages = list(messages)
        attempt = 0
        while True:
            try:
                return self.provider.chat(messages, model=model, params=params)
            except ProviderError as exc:
                if not is_transient_error(exc) or attempt >= self.max_retries:
                    raise
                attempt += 1
                self._wait(attempt)

    def stream(
        self,
        messages: Iterable[Any],
        *,
        model: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        messages = list(messages)
        attempt = 0
        while True:
            try:
                iterator = self.provider.stream(messages, model=model, params=params)
                first = next(iterator)
                break
            except StopIteration:
                return
            except ProviderError as exc:
                if not is_transient_error(exc) or attempt >= self.max_retries:
                    raise
                attempt += 1
                self._wait(attempt)
        yield first
        yield from iterator


def get_retrying_provider(name: str | None = None) -> RetryingProvider:
    """Resolve a provider (``name`` or ``LLM_PROVIDER``) wrapped in retries."""
    from app.services.providers.registry import get_provider

    return RetryingProvider(get_provider(name))


__all__ = [
    "DEFAULT_BASE_DELAY",
    "DEFAULT_MAX_DELAY",
    "DEFAULT_MAX_RETRIES",
    "TRANSIENT_PROVIDER_ERRORS",
    "RetryingProvider",
    "get_retrying_provider",
    "is_transient_error",
]
