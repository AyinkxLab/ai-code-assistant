"""Provider-agnostic LLM contract (issue #2).

Every vendor integration implements :class:`LLMProvider` and returns a uniform
:class:`ProviderResponse`. Failures are normalized into the :class:`ProviderError`
hierarchy, so routes and services never depend on a vendor SDK, HTTP status
code, or response shape.

Adding a new provider
---------------------
1. Subclass :class:`LLMProvider` in ``app/services/providers/<name>.py``, set
   ``name`` and ``models``, and implement ``chat`` and ``stream``.
2. Register it in ``app/services/providers/__init__.py`` with
   ``register_provider("<name>", YourProvider)``.
3. Select it with ``LLM_PROVIDER=<name>`` (or call ``get_provider("<name>")``).

Providers must only raise the exceptions in this module (or subclasses), and
must perform all network/SDK work internally so callers stay vendor-neutral.
"""

from __future__ import annotations

import logging
import random
import time
from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, ClassVar

logger = logging.getLogger(__name__)


def message_role(message: Any) -> str:
    """Read ``role`` from a dict-like or object message."""
    return message["role"] if isinstance(message, dict) else message.role


def message_content(message: Any) -> str:
    """Read ``content`` from a dict-like or object message."""
    return message["content"] if isinstance(message, dict) else message.content


def message_images(message: Any) -> list[dict]:
    """Read image attachments from a dict-like or object message.

    Each entry is ``{"content_type": str, "data": <base64 str>}``. Missing or
    ``None`` images yield an empty list.
    """
    if isinstance(message, dict):
        return list(message.get("images") or [])
    return list(getattr(message, "images", None) or [])


def normalize_messages(messages: Iterable[Any]) -> list[dict]:
    """Return ``messages`` as ``{"role", "content"[. "images"]}`` dicts."""
    normalized: list[dict] = []
    for message in messages:
        item: dict = {"role": message_role(message), "content": message_content(message)}
        images = message_images(message)
        if images:
            item["images"] = images
        normalized.append(item)
    return normalized


def prepare_messages(messages: Iterable[Any], *, supports_vision: bool) -> list[dict]:
    """Normalize messages, degrading images for text-only providers (#49).

    Vision-capable providers receive ``images`` unchanged and format them for
    their API. Text-only providers get a short note explaining that the
    attachment was omitted, so the model can still respond to the text.
    """
    prepared: list[dict] = []
    for message in normalize_messages(messages):
        images = message.pop("images", [])
        if images and supports_vision:
            message["images"] = images
        elif images:
            note = (
                f"[{len(images)} image attachment(s) omitted: this model does not "
                "support images]"
            )
            message["content"] = f"{message['content']}\n\n{note}".strip()
        prepared.append(message)
    return prepared


@dataclass(frozen=True)
class ProviderResponse:
    """Uniform, vendor-neutral result of a single completion."""

    content: str
    model: str | None = None
    prompt_tokens: int | None = None
    completion_tokens: int | None = None
    latency_seconds: float | None = None

    @property
    def total_tokens(self) -> int | None:
        """Total tokens used, or ``None`` when the provider reports no usage."""
        if self.prompt_tokens is None and self.completion_tokens is None:
            return None
        return (self.prompt_tokens or 0) + (self.completion_tokens or 0)

    def to_dict(self) -> dict[str, Any]:
        """Serialize the response for logging/telemetry."""
        return {
            "content": self.content,
            "model": self.model,
            "prompt_tokens": self.prompt_tokens,
            "completion_tokens": self.completion_tokens,
            "total_tokens": self.total_tokens,
            "latency_seconds": self.latency_seconds,
        }


class ProviderError(RuntimeError):
    """Base class for every LLM provider failure."""

    def __init__(self, message: str, *, provider: str | None = None) -> None:
        super().__init__(message)
        self.provider = provider


class ProviderConfigurationError(ProviderError):
    """The provider is missing or misconfigured (e.g. no API key)."""


class UnknownProviderError(ProviderConfigurationError):
    """The selected provider name is not registered."""


class ProviderAuthenticationError(ProviderError):
    """The provider rejected the supplied credentials."""


class ProviderRateLimitError(ProviderError):
    """The provider throttled the request."""


class ProviderUnavailableError(ProviderError):
    """The provider is unreachable or returned a server error."""


class ProviderResponseError(ProviderError):
    """The provider returned a response in an unexpected shape."""


class ProviderCircuitOpenError(ProviderUnavailableError):
    """The circuit breaker is open after repeated provider failures."""


class ProviderTimeoutError(ProviderUnavailableError):
    """The provider did not respond within the allotted time."""


#: Errors that may succeed if retried (transient failures).
RETRYABLE_ERRORS: tuple[type[ProviderError], ...] = (
    ProviderRateLimitError,
    ProviderUnavailableError,
    ProviderCircuitOpenError,
    ProviderTimeoutError,
)


def is_retryable(error: BaseException) -> bool:
    """Return ``True`` when ``error`` is a transient, retryable failure."""
    return isinstance(error, RETRYABLE_ERRORS)


class LLMProvider(ABC):
    """Abstract base class implemented by every concrete provider."""

    name: ClassVar[str] = "base"
    models: ClassVar[tuple[str, ...]] = ()
    #: Whether the provider needs a credential (environment variable or a
    #: user-stored key) before it can serve requests. Keyless providers such as
    #: the mock provider leave this ``False``; real providers set it ``True``.
    requires_key: ClassVar[bool] = False
    #: Whether the provider accepts image content blocks (issue #49).
    supports_vision: ClassVar[bool] = False
    #: Maximum number of attempts (including the first) for retryable errors.
    max_attempts: ClassVar[int] = 3
    #: Base delay in seconds for exponential backoff between retries.
    backoff_base_seconds: ClassVar[float] = 0.5
    #: Upper bound in seconds for a single backoff sleep.
    backoff_max_seconds: ClassVar[float] = 8.0
    #: Number of consecutive failures before the circuit breaker opens.
    circuit_breaker_threshold: ClassVar[int] = 5
    #: Seconds the circuit breaker stays open before allowing a probe.
    circuit_breaker_cooldown_seconds: ClassVar[float] = 30.0
    #: Whether to log retry attempts (useful for debugging in tests).
    log_retries: ClassVar[bool] = True

    @property
    def supported_models(self) -> tuple[str, ...]:
        """Models this provider accepts, used to populate the model selector.

        Providers only need to define ``models``; this alias gives the
        generation-settings UI (issue #12) a stable, explicit name.
        """
        return self.models

    def __init__(self) -> None:
        # Circuit-breaker state: consecutive failures and the monotonic time
        # until which the breaker stays open.
        self._consecutive_failures: int = 0
        self._circuit_open_until: float = 0.0

    @abstractmethod
    def chat(
        self,
        messages: Iterable[Any],
        *,
        model: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> ProviderResponse:
        """Return a complete :class:`ProviderResponse` for ``messages``."""

    @abstractmethod
    def stream(
        self,
        messages: Iterable[Any],
        *,
        model: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        """Yield incremental content chunks for ``messages``."""

    def _circuit_is_open(self) -> bool:
        """Return ``True`` while the breaker is tripped and cooling down."""
        return time.monotonic() < self._circuit_open_until

    def _circuit_remaining_seconds(self) -> float:
        """Seconds until the circuit breaker allows another probe."""
        return max(0.0, self._circuit_open_until - time.monotonic())

    def _record_success(self) -> None:
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def _record_failure(self) -> None:
        self._consecutive_failures += 1
        if self._consecutive_failures >= self.circuit_breaker_threshold:
            self._circuit_open_until = time.monotonic() + self.circuit_breaker_cooldown_seconds

    def _backoff_delay(self, attempt: int) -> float:
        """Jittered exponential backoff for the given zero-based ``attempt``."""
        base = self.backoff_base_seconds * (2**attempt)
        capped = min(base, self.backoff_max_seconds)
        # Full jitter: uniform in [0, capped] avoids thundering herds.
        return random.uniform(0.0, capped)

    def _reset_circuit(self) -> None:
        """Reset breaker state (used by tests and manual recovery)."""
        self._consecutive_failures = 0
        self._circuit_open_until = 0.0

    def _run_with_retry(self, operation: Any) -> Any:
        """Execute ``operation`` with bounded retries and backoff.

        Retryable errors are retried up to ``max_attempts`` with jittered
        exponential backoff. Permanent errors fail fast. A tripped circuit
        breaker short-circuits immediately with a clear message.
        """
        if self._circuit_is_open():
            remaining = self._circuit_remaining_seconds()
            raise ProviderCircuitOpenError(
                f"Provider '{self.name}' is temporarily unavailable after "
                f"repeated failures; retry in {remaining:.1f}s.",
                provider=self.name,
            )

        last_error: ProviderError | None = None
        for attempt in range(self.max_attempts):
            try:
                result = operation()
            except ProviderError as error:
                last_error = error
                if not is_retryable(error):
                    # Permanent failure: fail fast, do not trip the breaker.
                    raise
                self._record_failure()
                if attempt + 1 >= self.max_attempts:
                    break
                delay = self._backoff_delay(attempt)
                if self.log_retries:
                    logger.warning(
                        "Provider '%s' attempt %d/%d failed (%s); retrying in %.2fs",
                        self.name,
                        attempt + 1,
                        self.max_attempts,
                        type(error).__name__,
                        delay,
                    )
                time.sleep(delay)
            else:
                self._record_success()
                return result

        assert last_error is not None  # loop only exits after a failure
        raise last_error

    def chat_with_retry(
        self,
        messages: Iterable[Any],
        *,
        model: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> ProviderResponse:
        """Like :meth:`chat` but with bounded retries and backoff."""
        return self._run_with_retry(lambda: self.chat(messages, model=model, params=params))

    def stream_with_retry(
        self,
        messages: Iterable[Any],
        *,
        model: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        """Like :meth:`stream` but with bounded retries and backoff.

        Only the initial connection is retried; once chunks start flowing the
        generator is yielded through without re-running the request.
        """
        iterator = self._run_with_retry(lambda: self.stream(messages, model=model, params=params))
        yield from iterator

    def complete(self, messages: Iterable[Any], *, stream: bool = False) -> str:
        """Backward-compatible convenience returning just the content string."""
        return self.chat(messages).content
