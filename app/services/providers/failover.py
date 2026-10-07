"""Graceful LLM provider failover (issue #39).

:class:`FailoverProvider` decorates an *ordered* list of providers. Each entry is
expected to already be wrapped in a ``RetryingProvider``, so the layering is::

    FailoverProvider([RetryingProvider(openai), RetryingProvider(anthropic)])

The retry layer absorbs transient blips within one provider (bounded by
``LLM_MAX_RETRIES``); only when that provider is still failing does the failover
layer move to the next one. Two invariants keep the behaviour safe:

* **Only transient failures fail over.** A transport error, HTTP 429 or 5xx
  (``ProviderRateLimitError`` / ``ProviderUnavailableError``) is retryable and
  may be routed to the next provider. Authentication, configuration/validation
  and malformed-response errors are terminal: they are re-raised immediately so
  a bad key or a malformed request is never papered over by another vendor.
* **Partial output is never replayed.** Once a stream has yielded a chunk, the
  attempt owns the answer: an error after that point is surfaced rather than
  failed over, because a second provider would duplicate content the client
  already rendered. This mirrors the retry layer, which also treats the first
  emitted chunk as a committed attempt.

Every attempt is recorded exactly once as a :class:`ProviderAttempt` and emitted
as a ``chat.provider_attempt`` audit event carrying a single logical request id,
so an all-failed chain stays explainable: :meth:`FailoverProvider.diagnostics`
returns the chain, the serving provider, and the per-attempt outcome.
"""

from __future__ import annotations

import time
import uuid
from collections.abc import Callable, Iterable, Iterator
from dataclasses import asdict, dataclass
from typing import Any

from app.services import chat_audit
from app.services.providers.base import (
    LLMProvider,
    ProviderConfigurationError,
    ProviderError,
    ProviderResponse,
)
from app.services.providers.retry import (
    RetryingProvider,
    classify_error,
    is_transient_error,
)


class ProviderFailoverError(ProviderError):
    """Every provider in the failover chain failed.

    Carries the per-attempt diagnostics so callers (and the audit log) can show
    *which* providers were tried and why each one was abandoned. It subclasses
    :class:`ProviderError` but not a transient subclass, so wrapping a failover
    chain in another retry layer would fail fast instead of retrying the whole
    chain.
    """

    def __init__(
        self,
        message: str,
        *,
        attempts: Iterable["ProviderAttempt"] = (),
        provider: str | None = None,
    ) -> None:
        super().__init__(message, provider=provider)
        self.attempts = tuple(attempts)


@dataclass(frozen=True)
class ProviderAttempt:
    """One provider's outcome within a failover chain."""

    index: int
    provider: str
    status: str  # "success" | "error"
    error: str | None = None
    category: str | None = None
    transient: bool = False
    latency_ms: float | None = None
    stream: bool = False
    served: bool = False
    partial: bool = False

    def to_dict(self) -> dict[str, Any]:
        """Serialize the attempt for diagnostics/tests."""
        return asdict(self)


def new_request_id() -> str:
    """Return the logical request id shared by every attempt in a chain.

    Inside an HTTP request the chat-audit correlation id is reused so failover
    events line up with the rest of the request; elsewhere (background jobs,
    tests) a fresh id is generated.
    """
    return chat_audit.current_request_id() or uuid.uuid4().hex


class FailoverProvider(LLMProvider):
    """Try an ordered list of providers until one serves the request."""

    def __init__(
        self,
        providers: Iterable[LLMProvider],
        *,
        request_id: str | None = None,
        on_attempt: Callable[[ProviderAttempt], None] | None = None,
    ) -> None:
        providers = tuple(providers)
        if not providers:
            raise ProviderConfigurationError("A failover chain needs at least one provider.")
        self.providers = providers
        self.name = "+".join(provider.name for provider in providers)
        seen: list[str] = []
        for provider in providers:
            for model in provider.models:
                if model not in seen:
                    seen.append(model)
        self.models = tuple(seen)
        self.requires_key = any(getattr(provider, "requires_key", False) for provider in providers)
        self.supports_vision = any(
            getattr(provider, "supports_vision", False) for provider in providers
        )
        self.request_id = request_id or new_request_id()
        self.attempts: list[ProviderAttempt] = []
        self.served_provider: str | None = None
        self.partial_provider: str | None = None
        self._on_attempt = on_attempt

    @property
    def chain(self) -> tuple[str, ...]:
        """The ordered provider names this chain will try."""
        return tuple(provider.name for provider in self.providers)

    def _record(
        self,
        index: int,
        provider: str,
        status: str,
        exc: ProviderError | None,
        started: float,
        *,
        stream: bool,
        served: bool = False,
        partial: bool = False,
    ) -> ProviderAttempt:
        attempt = ProviderAttempt(
            index=index,
            provider=provider,
            status=status,
            error=type(exc).__name__ if exc is not None else None,
            category=classify_error(exc) if exc is not None else None,
            transient=is_transient_error(exc) if exc is not None else False,
            latency_ms=round((time.monotonic() - started) * 1000, 1),
            stream=stream,
            served=served,
            partial=partial,
        )
        self.attempts.append(attempt)
        chat_audit.log_event(
            "chat.provider_attempt",
            request_id=self.request_id,
            provider=provider,
            attempt=index + 1,
            status=status,
            error=attempt.error,
            error_category=attempt.category,
            transient=attempt.transient,
            stream=stream,
            served=served,
            partial=partial,
            latency_ms=attempt.latency_ms,
            chain=",".join(self.chain),
        )
        if self._on_attempt is not None:
            self._on_attempt(attempt)
        return attempt

    def _all_failed(self, errors: list[tuple[str, ProviderError]]) -> ProviderError:
        """Build the terminal error once every provider has been tried."""
        if len(errors) == 1:
            # A single-provider chain must behave exactly like the bare provider,
            # so the original error (and its user-facing message) is preserved.
            return errors[0][1]
        detail = "; ".join(
            f"{name} ({classify_error(exc)}): {exc}" for name, exc in errors
        )
        chain = ", ".join(self.chain)
        return ProviderFailoverError(
            "All configured LLM providers failed "
            f"[{chain}] for request {self.request_id}: {detail}",
            attempts=tuple(self.attempts),
        )

    def chat(
        self,
        messages: Iterable[Any],
        *,
        model: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> ProviderResponse:
        messages = list(messages)
        errors: list[tuple[str, ProviderError]] = []
        for index, provider in enumerate(self.providers):
            started = time.monotonic()
            try:
                response = provider.chat(messages, model=model, params=params)
            except ProviderError as exc:
                self._record(index, provider.name, "error", exc, started, stream=False)
                if not is_transient_error(exc):
                    # Authentication/validation/policy/config errors are terminal.
                    raise
                errors.append((provider.name, exc))
                continue
            self._record(
                index, provider.name, "success", None, started, stream=False, served=True
            )
            self.served_provider = provider.name
            return response
        raise self._all_failed(errors)

    def stream(
        self,
        messages: Iterable[Any],
        *,
        model: str | None = None,
        params: dict[str, Any] | None = None,
    ) -> Iterator[str]:
        messages = list(messages)
        errors: list[tuple[str, ProviderError]] = []
        for index, provider in enumerate(self.providers):
            started = time.monotonic()
            emitted = False
            try:
                for chunk in provider.stream(messages, model=model, params=params):
                    emitted = True
                    yield chunk
            except ProviderError as exc:
                if emitted:
                    # Partial output is already committed to the client; moving
                    # to another provider would duplicate the answer, so the
                    # error is surfaced instead of replayed.
                    self._record(
                        index,
                        provider.name,
                        "error",
                        exc,
                        started,
                        stream=True,
                        partial=True,
                    )
                    self.partial_provider = provider.name
                    raise
                self._record(index, provider.name, "error", exc, started, stream=True)
                if not is_transient_error(exc):
                    raise
                errors.append((provider.name, exc))
                continue
            self._record(
                index, provider.name, "success", None, started, stream=True, served=True
            )
            self.served_provider = provider.name
            return
        raise self._all_failed(errors)

    def diagnostics(self) -> dict[str, Any]:
        """Report the chain, the serving provider, and every attempt.

        This is the machine-readable answer to "who actually served the
        request?" and, on the all-failed path, "why did every provider fail?".
        """
        return {
            "request_id": self.request_id,
            "chain": list(self.chain),
            "served_provider": self.served_provider,
            "partial_provider": self.partial_provider,
            "failed": self.served_provider is None,
            "attempts": [attempt.to_dict() for attempt in self.attempts],
        }


def build_failover_chain(
    names: Iterable[str],
    *,
    build: Callable[[str], LLMProvider],
    wrap: Callable[[LLMProvider], LLMProvider] | None = None,
    request_id: str | None = None,
    on_attempt: Callable[[ProviderAttempt], None] | None = None,
) -> FailoverProvider:
    """Resolve ``names`` and wrap them into a :class:`FailoverProvider`.

    ``build`` constructs one provider per name (the app passes its registry- and
    stored-key-aware ``build_provider``); ``wrap`` defaults to
    :class:`RetryingProvider` so each entry retries within itself before the
    chain fails over. ``wrap``/``build`` are injected so call sites keep testable
    module-level seams instead of being bypassed.
    """
    wrap = wrap or RetryingProvider
    providers = [wrap(build(name)) for name in names]
    if not providers:
        raise ProviderConfigurationError("No LLM providers are configured for the failover chain.")
    return FailoverProvider(providers, request_id=request_id, on_attempt=on_attempt)


__all__ = [
    "FailoverProvider",
    "ProviderAttempt",
    "ProviderFailoverError",
    "build_failover_chain",
    "new_request_id",
]
