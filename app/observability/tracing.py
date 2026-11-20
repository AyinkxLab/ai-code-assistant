"""Bounded distributed tracing for requests, services and plugin calls (#703).

A trace is a list of spans sharing the request id assigned by
``app.services.chat_audit`` (``X-Request-ID``). Spans are recorded only inside a
request and are written as one JSON object per span to the
``app.observability.trace`` logger, so an existing JSON log pipeline can render
them without a new backend. The most recent traces are also kept in a bounded
in-process buffer for local inspection.

Bounded by construction:

* at most :data:`MAX_SPANS_PER_TRACE` spans per trace;
* at most :data:`MAX_SPAN_ATTRIBUTES` attributes per span;
* attribute keys ≤ 32 characters and values ≤ 64 characters;
* attribute names matching :data:`SENSITIVE_KEY` are dropped entirely.

Callers must pass identifiers and counters only. Prompt text, message bodies,
tokens, keys and request bodies are never accepted by the instrumentation; the
filter is a second line of defence, not a licence to pass content.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from collections import deque
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass, field
from time import perf_counter

from flask import Flask, g, has_request_context, request

LOGGER = logging.getLogger("app.observability.trace")

MAX_SPANS_PER_TRACE = 32
MAX_SPAN_ATTRIBUTES = 8
MAX_ATTRIBUTE_LENGTH = 64
MAX_ATTRIBUTE_KEY_LENGTH = 32
MAX_SPAN_NAME_LENGTH = 64
MAX_RECENT_TRACES = 100
TRACE_ID_HEADER = "X-Trace-ID"

#: Attribute names that may carry user content or credentials. They are never
#: recorded, even when a caller passes them by mistake.
SENSITIVE_KEY = re.compile(
    r"(prompt|content|message|body|query|secret|token|password|authorization|api[_-]?key)",
    re.IGNORECASE,
)

_SPANS: ContextVar[list[Span] | None] = ContextVar("trace_spans", default=None)
_STACK: ContextVar[tuple[str, ...]] = ContextVar("trace_stack", default=())
_RECENT_TRACES: deque[dict] = deque(maxlen=MAX_RECENT_TRACES)


@dataclass
class Span:
    """One finished span. ``to_dict`` is the log/viewer record."""

    name: str
    trace_id: str
    span_id: str
    parent_span_id: str | None
    duration_ms: float
    attributes: dict[str, str] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "event": "span",
            "trace_id": self.trace_id,
            "span_id": self.span_id,
            "parent_span_id": self.parent_span_id,
            "name": self.name,
            "duration_ms": round(self.duration_ms, 3),
            "attributes": self.attributes,
        }


def _enabled() -> bool:
    try:
        from flask import current_app

        return bool(current_app.config.get("TRACING_ENABLED", True))
    except RuntimeError:
        return False


def _bounded_attributes(attributes: dict[str, object]) -> dict[str, str]:
    """Return at most ``MAX_SPAN_ATTRIBUTES`` safe, length-bounded attributes."""
    bounded: dict[str, str] = {}
    for key, value in attributes.items():
        if len(bounded) >= MAX_SPAN_ATTRIBUTES:
            break
        if value is None:
            continue
        name = str(key)
        if not name or SENSITIVE_KEY.search(name):
            continue
        text = value if isinstance(value, str) else str(value)
        bounded[name[:MAX_ATTRIBUTE_KEY_LENGTH]] = text[:MAX_ATTRIBUTE_LENGTH]
    return bounded


def _trace_id() -> str:
    return current_trace_id() or uuid.uuid4().hex


def current_request_id() -> str | None:
    """The current request's correlation id, or ``None`` outside a request.

    Read directly from ``g`` so importing this module cannot pull in the
    service package (which imports the provider tracer) and create a cycle.
    """
    if not has_request_context():
        return None
    try:
        return getattr(g, "request_id", None)
    except RuntimeError:
        return None


def _spans() -> list[Span] | None:
    if not has_request_context() or not _enabled():
        return None
    spans = _SPANS.get()
    if spans is None:
        spans = []
        _SPANS.set(spans)
    return spans


@contextmanager
def trace_span(name: str, **attributes: object):
    """Record a span for the duration of the ``with`` block.

    Outside a request, when tracing is disabled, or when the trace already has
    :data:`MAX_SPANS_PER_TRACE` spans the block still runs; it is simply not
    recorded. The block yields the open :class:`Span` or ``None``.
    """
    spans = _spans()
    if spans is None or len(spans) >= MAX_SPANS_PER_TRACE:
        yield None
        return

    stack = _STACK.get()
    span = Span(
        name=str(name)[:MAX_SPAN_NAME_LENGTH],
        trace_id=_trace_id(),
        span_id=uuid.uuid4().hex[:16],
        parent_span_id=stack[-1] if stack else None,
        duration_ms=0.0,
        attributes=_bounded_attributes(attributes),
    )
    started = perf_counter()
    token = _STACK.set((*stack, span.span_id))
    try:
        yield span
    finally:
        span.duration_ms = (perf_counter() - started) * 1000.0
        _STACK.reset(token)
        spans.append(span)


def current_spans() -> list[Span]:
    """Return the spans recorded so far in the current request."""
    return list(_SPANS.get() or [])


def current_trace_id() -> str | None:
    """Return the trace id for the current request, or ``None`` outside one."""
    return current_request_id() if has_request_context() else None


def recent_traces() -> list[dict]:
    """Return the most recent recorded traces (bounded, newest last)."""
    return list(_RECENT_TRACES)


def reset_tracing() -> None:
    """Clear per-request and in-process trace state (tests, operators)."""
    _SPANS.set(None)
    _STACK.set(())
    _RECENT_TRACES.clear()


def _finish_request_trace() -> None:
    spans = current_spans()
    if not spans:
        return
    trace_id = spans[0].trace_id
    payload = {
        "event": "trace",
        "trace_id": trace_id,
        "request_id": current_request_id(),
        "method": request.method,
        "endpoint": request.url_rule.rule if request.url_rule else "unmatched",
        "span_count": len(spans),
        "spans": [span.to_dict() for span in spans],
    }
    _RECENT_TRACES.append(payload)
    for span in spans:
        LOGGER.info(json.dumps(span.to_dict(), separators=(",", ":"), sort_keys=True))


def init_tracing(app: Flask) -> None:
    """Attach trace flushing and the response trace header to ``app``."""
    if not bool(app.config.get("TRACING_ENABLED", True)):
        return

    @app.before_request
    def _start_trace() -> None:
        _SPANS.set([])
        _STACK.set(())

    @app.after_request
    def _flush_trace(response):
        trace_id = current_trace_id()
        if trace_id:
            response.headers[TRACE_ID_HEADER] = trace_id
        try:
            _finish_request_trace()
        except Exception:  # pragma: no cover - tracing must never fail a request
            LOGGER.debug("Could not flush trace", exc_info=True)
        return response


__all__ = [
    "MAX_ATTRIBUTE_LENGTH",
    "MAX_RECENT_TRACES",
    "MAX_SPANS_PER_TRACE",
    "MAX_SPAN_ATTRIBUTES",
    "TRACE_ID_HEADER",
    "Span",
    "current_spans",
    "current_trace_id",
    "init_tracing",
    "recent_traces",
    "reset_tracing",
    "trace_span",
]
