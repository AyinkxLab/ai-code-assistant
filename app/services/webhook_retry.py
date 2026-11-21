"""Bounded exponential-backoff retries for webhook deliveries (#670).

Deliveries retry failed attempts with exponentially growing, jittered delays.
Both the number of attempts and the total time spent sleeping are capped, so a
misbehaving endpoint can never keep a worker busy forever.

The module is transport-agnostic: callers pass a ``send`` callable. Each attempt
is reported through ``on_attempt`` so the caller can persist a delivery-log row
with the status, response code, and latency (see ``webhook_deliveries``).
"""

from __future__ import annotations

import random
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

STATUS_SUCCESS = "success"
STATUS_FAILURE = "failure"

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 30.0
DEFAULT_TOTAL_DELAY_CAP = 60.0


@dataclass
class RetryPolicy:
    """Caps and delay shape for a delivery retry sequence."""

    max_attempts: int = DEFAULT_MAX_ATTEMPTS
    base_delay: float = DEFAULT_BASE_DELAY
    max_delay: float = DEFAULT_MAX_DELAY
    total_delay_cap: float = DEFAULT_TOTAL_DELAY_CAP

    def __post_init__(self) -> None:
        if self.max_attempts < 1:
            raise ValueError("max_attempts must be at least 1")
        if self.base_delay < 0 or self.max_delay < 0 or self.total_delay_cap < 0:
            raise ValueError("delays must be non-negative")

    def delay_for(self, attempt: int, jitter: Callable[[], float] = random.random) -> float:
        """Return the jittered delay before retry ``attempt`` (1-based).

        The exponential term is capped at ``max_delay``; jitter is full-jitter
        in ``[0, 1]`` so a fleet of clients does not retry in lockstep.
        """
        if attempt < 1:
            return 0.0
        exponential = self.base_delay * (2 ** (attempt - 1))
        capped = min(exponential, self.max_delay)
        factor = max(0.0, min(1.0, jitter()))
        return capped * factor


@dataclass
class DeliveryAttempt:
    """The observable outcome of one attempt."""

    attempt: int
    status: str
    response_code: int | None = None
    latency_ms: float | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "attempt": self.attempt,
            "status": self.status,
            "response_code": self.response_code,
            "latency_ms": self.latency_ms,
            "error": self.error,
        }


@dataclass
class DeliveryResult:
    """The result of a complete (possibly retried) delivery."""

    success: bool
    attempts: list[DeliveryAttempt] = field(default_factory=list)
    response_code: int | None = None
    error: str | None = None


def _status_code_of(response: Any) -> int | None:
    code = getattr(response, "status_code", None)
    if isinstance(code, bool) or not isinstance(code, int):
        return None
    return code


def _is_success(response: Any) -> bool:
    code = _status_code_of(response)
    return code is not None and 200 <= code < 300


def deliver_with_retry(
    send: Callable[[], Any],
    *,
    policy: RetryPolicy | None = None,
    status_code: Callable[[Any], int | None] = _status_code_of,
    is_success: Callable[[Any], bool] = _is_success,
    sleep: Callable[[float], None] = time.sleep,
    jitter: Callable[[], float] = random.random,
    on_attempt: Callable[[DeliveryAttempt], None] | None = None,
    clock: Callable[[], float] = time.monotonic,
) -> DeliveryResult:
    """Call ``send`` until it succeeds, attempts are exhausted, or time is up.

    ``send`` may return any object with a ``status_code`` attribute, or raise an
    exception. Exceptions are recorded as failures (the exception type name is
    kept; the message is not, to avoid leaking endpoint details).
    """
    policy = policy or RetryPolicy()
    attempts: list[DeliveryAttempt] = []
    total_delay = 0.0
    last_code: int | None = None
    last_error: str | None = None

    for number in range(1, policy.max_attempts + 1):
        started = clock()
        try:
            response = send()
        except Exception as exc:
            latency_ms = round((clock() - started) * 1000, 1)
            last_code = None
            last_error = type(exc).__name__
            attempt = DeliveryAttempt(
                attempt=number,
                status=STATUS_FAILURE,
                response_code=None,
                latency_ms=latency_ms,
                error=last_error,
            )
        else:
            latency_ms = round((clock() - started) * 1000, 1)
            code = status_code(response)
            if is_success(response):
                attempt = DeliveryAttempt(
                    attempt=number,
                    status=STATUS_SUCCESS,
                    response_code=code,
                    latency_ms=latency_ms,
                    error=None,
                )
                attempts.append(attempt)
                if on_attempt is not None:
                    on_attempt(attempt)
                return DeliveryResult(success=True, attempts=attempts, response_code=code)
            last_code = code
            last_error = f"HTTP {code}" if code is not None else "delivery failed"
            attempt = DeliveryAttempt(
                attempt=number,
                status=STATUS_FAILURE,
                response_code=code,
                latency_ms=latency_ms,
                error=last_error,
            )

        attempts.append(attempt)
        if on_attempt is not None:
            on_attempt(attempt)
        if number >= policy.max_attempts:
            break

        delay = policy.delay_for(number, jitter)
        if delay <= 0:
            continue
        if total_delay + delay > policy.total_delay_cap:
            break
        total_delay += delay
        sleep(delay)

    return DeliveryResult(
        success=False, attempts=attempts, response_code=last_code, error=last_error
    )


__all__ = [
    "DEFAULT_BASE_DELAY",
    "DEFAULT_MAX_ATTEMPTS",
    "DEFAULT_MAX_DELAY",
    "DEFAULT_TOTAL_DELAY_CAP",
    "STATUS_FAILURE",
    "STATUS_SUCCESS",
    "DeliveryAttempt",
    "DeliveryResult",
    "RetryPolicy",
    "deliver_with_retry",
]
