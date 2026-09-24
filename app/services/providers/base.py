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

from abc import ABC, abstractmethod
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from typing import Any, ClassVar


def message_role(message: Any) -> str:
    """Read ``role`` from a dict-like or object message."""
    return message["role"] if isinstance(message, dict) else message.role


def message_content(message: Any) -> str:
    """Read ``content`` from a dict-like or object message."""
    return message["content"] if isinstance(message, dict) else message.content


def normalize_messages(messages: Iterable[Any]) -> list[dict[str, str]]:
    """Return ``messages`` as a list of ``{"role", "content"}`` dicts."""
    return [{"role": message_role(m), "content": message_content(m)} for m in messages]


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


class LLMProvider(ABC):
    """Abstract base class implemented by every concrete provider."""

    name: ClassVar[str] = "base"
    models: ClassVar[tuple[str, ...]] = ()

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

    def complete(self, messages: Iterable[Any], *, stream: bool = False) -> str:
        """Backward-compatible convenience returning just the content string."""
        return self.chat(messages).content
