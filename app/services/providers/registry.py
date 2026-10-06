"""Provider registry: register factories by name and resolve by config (#2).

Providers register themselves on import (see ``app/services/providers/__init__``)
and are resolved through ``LLM_PROVIDER`` or an explicit name. Unknown names
raise :class:`UnknownProviderError` with the list of available providers, so the
failure is clear and user-facing rather than a silent fallback.

This module also owns the resilience policy for transient provider failures:
error classification (retryable vs. permanent), bounded retries with jittered
exponential backoff, and a circuit-breaker guard that opens when the same
provider fails repeatedly. The policy is exposed as :func:`call_with_retry` so
chat services can wrap provider calls without duplicating the logic.
"""

from __future__ import annotations

import os
import random
import time
from collections.abc import Callable

from app.services.providers.base import LLMProvider, UnknownProviderError

_FACTORIES: dict[str, Callable[[], LLMProvider]] = {}


# --- Retry / backoff / circuit-breaker policy -------------------------------------

DEFAULT_MAX_ATTEMPTS = 3
ENV_MAX_ATTEMPTS = "LLM_MAX_RETRIES"
DEFAULT_BASE_DELAY_S = 0.25
ENV_BASE_DELAY_S = "LLM_RETRY_BASE_DELAY_S"
DEFAULT_MAX_DELAY_S = 8.0
ENV_MAX_DELAY = "LLM_RETRY_MAX_DELAY_S"
DEFAULT_CIRCUIT_THRESHOLD = 3
ENV_CIRCUIT_THRESHOLD = "LLM_CIRCUIT_THRESHOLD"
DEFAULT_CIRCUIT_RESET_S = 60.0
ENV_CIRCUIT_RESET_S = "LLM_CIRCUIT_RESET_S"


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.getenv(name, "") or default)
    except (TypeError, ValueError):
        return default


def max_attempts() -> int:
    """Return the bounded number of attempts (at least 1)."""
    return max(1, _env_int(ENV_MAX_ATTEMPTS, DEFAULT_MAX_ATTEMPTS))


def _base_delay() -> float:
    return max(0.0, _env_float(ENV_BASE_DELAY_S, DEFAULT_BASE_DELAY_S))


def _max_delay() -> float:
    return max(0.0, _env_float(ENV_MAX_DELAY, DEFAULT_MAX_DELAY_S))


def _circuit_threshold() -> int:
    return max(1, _env_int(ENV_CIRCUIT_THRESHOLD, DEFAULT_CIRCUIT_THRESHOLD))


def _circuit_reset_s() -> float:
    return max(0.0, _env_float(ENV_CIRCUIT_RESET_S, DEFAULT_CIRCUIT_RESET_S))


def backoff_delay(attempt: int, *, random_func: Callable[[], float] = random.random) -> float:
    """$exponential backoff with full jitter for attempt index *`attempt`* (0-based).

    The delay is bounded by :func:`_max_delay` and never negative, so the
    retry loop can not spine unbounded or tight. Full jitter keeps concurrent
    clients from retrying in lockstep.
    """
    attempt = max(0, int(attempt))
    ceiling = min(_max_delay(), _base_delay() * (2**attempt))
    if ceiling <= 0:
        return 0.0
    try:
        fraction = float(random_func())
    except Exception:
        fraction = 0.0
    fraction = min(1.0, max(0.0, fraction))
    return fraction * ceiling


def classify_error(exc: BaseException) -> str:
    """Classify a provider error as `retryable` or `permanent`.

    Retryable: HTTP 429 (rate limit), 5xx (server faults), timeouts and
    connection drops. Permanent: 401/403 (invalid/missing key), 400 / 422
    (bad request), 404 and other 4xx client errors. Unknown errors default to
    `retryable` so a transient glitch is given a bounded chance to recover.
    """
    status = getattr(exc, "status_code", None)
    if status is None:
        status = getattr(getattr(exc, "response", None), "status_code", None)
    try:
        status = int(status)
    except (TypeError, ValueError):
        status = None

    if status is not None:
        if status == 429 or status >= 500:
            return "retryable"
        if 400 <= status < 500:
            return "permanent"

    name = type(exc).__name__.lower()
    message = str(exc).lower()
    if "invalid_api_key" in message or "invalid api key" in message:
        return "permanent"
    if "timeout" in name or "timed" in message or "timeout" in message:
        return "retryable"
    if "connection" in name or "connection" in message:
        return "retryable"
    if "rate" in message and "limit" in message:
        return "retryable"
    return "retryable"


def is_retryable(exc: BaseException) -> bool:
    """Convenience wrapper around :func:`classify_error`."""
    return classify_error(exc) == "retryable"


class ProviderError(Exception):
    """Error raised when a provider call exhausts its retry budget.

    Carries the classification and attempt count so the chat layer can persist
    a visible error state on the message and surface it through the API.
    """

    def __init__(
        self,
        message: str,
        *,
        provider: str | None = None,
        kind: str = "retryable",
        attempts: int = 1,
        cause: BaseException | None = None,
    ):
        super().__init__(message)
        self.provider = provider
        self.kind = kind
        self.attempts = attempts
        self.cause = cause

    def to_dict(self) -> dict:
        return {
            "error": str(self),
            "kind": self.kind,
            "provider": self.provider,
            "attempts": self.attempts,
        }


class CircuitOpenError(ProviderError):
    """Raised when the circuit breaker is open for a provider."""

    def __init__(self, message: str, *, provider: str | None = None, retry_after: float = 0.0):
        super().__init__(message, provider=provider, kind="circuit_open", attempts=0)
        self.retry_after = retry_after

    def to_dict(self) -> dict:
        data = super().to_dict()
        data["retry_after"] = self.retry_after
        return data


class _CircuitState:
    __slots__ = ("failures", "opened_at")

    def __init__(self):
        self.failures = 0
        self.opened_at = 0.0


_CIRCUITS: dict[str, _CircuitState] = {}


def _circuit_for(provider: str | None) -> _CircuitState:
    key = (provider or "").strip().lower()
    state = _CIRCUITS.get(key)
    if state is None:
        state = _CircuitState()
        _CIRCUITS[key] = state
    return state


def _circuit_open(state: _CircuitState, now: float) -> bool:
    if state.opened_at <= 0:
        return False
    if now - state.opened_at >= _circuit_reset_s():
        state.failures = 0
        state.opened_at = 0.0
        return False
    return True


def _record_success(provider: str | None) -> None:
    state = _circuit_for(provider)
    state.failures = 0
    state.opened_at = 0.0


def _record_failure(provider: str | None, now: float) -> None:
    state = _circuit_for(provider)
    state.failures += 1
    if state.failures >= _circuit_threshold():
        state.opened_at = now


def circuit_status(provider: str | None = None, now: float | None = None) -> dict:
    """Report the circuit-breaker state for a provider (used by tests/UI)."""
    now = time.monotonic() if now is None else now
    state = _circuit_for(provider)
    open = _circuit_open(state, now)
    return {
        "provider": (provider or "").strip().lower(),
        "failures": state.failures,
        "open": open,
        "retry_after": max(0.0, _circuit_reset_s() - (now - state.opened_at)) if open else 0.0,
    }


def reset_circuit(provider: str | None = None) -> None:
    """Clear circuit-breaker state (primarily for tests)."""
    if provider is None:
        _CIRCUITS.clear()
        return
    _CIRCUITS.pop((provider or "").strip().lower(), None)


def call_with_retry(
    func: Callable[[], object],
    *,
    provider: str | None = None,
    max_attempts_limit: int | None = None,
    sleep: Callable[[float], None] = time.sleep,
    random_func: Callable[[], float] = random.random,
) -> object:
    """Call ``func`` with bounded retries and jittered exponential backoff.

    Retryable failures (429/5xx/timeout/connection errors) are retried up to
    the configured max (default 3). Permanent failures (401/400/422/...) fail
    fast. When the same provider fails repeatedly the circuit breaker opens and
    further calls raise :class:`CircuitOpenError` without hitting the provider.

    On exhaustion a ProviderError is raised carrying the attempt count and
    cause, so the caller can persist an error state on the message.
    """
    attempts_limit = max(1, int(max_attempts_limit or max_attempts()))
    now = time.monotonic()
    state = _circuit_for(provider)
    if _circuit_open(state, now):
        retry_after = max(0.0, _circuit_reset_s() - (now - state.opened_at))
        raise CircuitOpenError(
            f"Provider '{provider or 'unknown'}' is temporarily unavailable; "
            f"retry in {retry_after:.1f}s.",
            provider=provider,
            retry_after=retry_after,
        )

    last_exc: BaseException | None = None
    for attempt in range(attempts_limit):
        try:
            result = func()
        except Exception as exc:
            last_exc = exc
            kind = classify_error(exc)
            if kind != "retryable":
                _record_failure(provider, time.monotonic())
                raise ProviderError(
                    f"Provider '{provider or 'unknown'}' rejected the request: {exc}",
                    provider=provider,
                    kind="permanent",
                    attempts=attempt + 1,
                    cause=exc,
                ) from exc
            _record_success(provider)
            return result

        if attempt + 1 >= attempts_limit:
            break
        delay = backoff_delay(attempt, random_func=random_func)
        if delay > 0:
            sleep(delay)

    _record_failure(provider, time.monotonic())
    message = (
        f"Provider '{provider or 'unknown'}' failed after {attempts_limit} "
        f"attempt{'s' if attempts_limit != 1 else ''}: {last_exc}"
    )
    raise ProviderError(
        message,
        provider=provider,
        kind="retryable",
        attempts=attempts_limit,
        cause=last_exc,
    ) from last_exc


# --- Registration -------------------------------------------------------------------------------


def register_provider(name: str, factory: Callable[[], LLMProvider]) -> None:
    """Register ``factory`` under ``name`` (case-insensitive)."""
    _FACTORIES.pop((name or "").strip().lower(), None)
    _FACTORIES[(name or "").strip().lower()] = factory


def unregister_provider(name: str) -> None:
    """Remove a provider registration (primarily for tests)."""
    _FACTORIES.pop((name or "").strip().lower(), None)


def available_providers() -> list[str]:
    """Return the sorted names of every registered provider."""
    return sorted(_FACTORIES)


def resolve_provider_name(name: str | None = None) -> str:
    """Resolve the provider name from an argument or the ``LLM_PROVIDER`` env."""
    return (name or os.getenv("LLM_PROVIDER") or "mock").strip().lower()


def _current_user_or_none():
    """Return the logged-in user when there is an active request context (#30)."""
    try:
        from flask_login import current_user
    except Exception:  # pragma: no cover - flask_login always present in the app
        return None
    try:
        if getattr(current_user, "is_authenticated", False):
            return current_user
    except Exception:  # no request context
        return None
    return None


def _stored_api_key(user, provider_name: str) -> str | None:
    """Decrypt the user's active stored key for ``provider_name``, or ``None``.

    Imports are lazy so the registry stays free of model/app imports at module
    load time (avoids circular imports during app startup). Decryption lives
    here — the provider service layer — and the plaintext never leaves it.
    """
    if user is None:
        return None
    try:
        from app.models import ApiKey
        from app.services.api_keys import decrypt_for_use
    except Exception:  # pragma: no cover - defensive
        return None
    key = (
        ApiKey.query.filter_by(
            user_id=getattr(user, "id", None), provider=provider_name, is_active=True
        )
        .order_by(ApiKey.created_at.desc())
        .first()
    )
    if key is None:
        return None
    try:
        return decrypt_for_use(key)
    except Exception:  # pragma: no cover - rotated secret / tampered row
        return None


def get_provider(name: str | None = None, *, user=None) -> LLMProvider:
    """Instantiate the provider selected by ``name`` (or ``LLM_PROVIDER``).

    When the provider requires a key that the environment does not supply, the
    signed-in user's stored (encrypted) key is decrypted and injected here, in
    the provider service layer, so a key added through the API-key UI unlocks
    chat without a restart (issues #25, #30). ``user`` may be passed explicitly;
    otherwise the current request's logged-in user is used when available.

    Raises :class:`UnknownProviderError` when the provider is not registered.
    """
    resolved = resolve_provider_name(name)
    factory = _FACTORIES.get(resolved)
    if factory is None:
        available = ", ".join(available_providers()) or "none"
        raise UnknownProviderError(
            f"Unknown LLM provider '{resolved}'. Available providers: {available}.",
            provider=resolved,
        )
    provider = factory()
    resolved_user = user if user is not None else _current_user_or_none()
    if (
        resolved_user is not None
        and getattr(provider, "requires_key", False)
        and not getattr(provider, "api_key", "")
    ):
        stored = _stored_api_key(resolved_user, resolved)
        if stored:
            try:
                provider = factory(api_key=stored)
            except TypeError:
                provider.api_key = stored
    return provider


def provider_status(user=None, name: str | None = None) -> dict:
    """Report whether the selected provider is ready to serve requests.

    Returns a small dict the UI can act on::

        {"provider": "openai", "requires_key": True,
         "configured": False, "reason": "missing_key"}

    ``reason`` is ``None``(keyless provider or env key present),
    ``"environment"``, ``"stored_key"``, ``"missing_key"`` or
    ``"unknown_provider"``.
    """
    resolved = resolve_provider_name(name)
    factory = _FACTORIES.get(resolved)
    if factory is None:
        return {
            "provider": resolved,
            "requires_key": True,
            "configured": False,
            "reason": "unknown_provider",
        }
    probe = factory()
    if not getattr(probe, "requires_key", False):
        return {"provider": resolved, "requires_key": False, "configured": True, "reason": None}
    if getattr(probe, "api_key", ""):
        return {
            "provider": resolved,
            "requires_key": True,
            "configured": True,
            "reason": "environment",
        }
    if _stored_api_key(user, resolved):
        return {
            "provider": resolved,
            "requires_key": True,
            "configured": True,
            "reason": "stored_key",
        }
    return {
        "provider": resolved,
        "requires_key": True,
        "configured": False,
        "reason": "missing_key",
    }
