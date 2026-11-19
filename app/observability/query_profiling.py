"""Per-request query profiling and the slow-query log (#696).

Every SQL statement executed inside a request is counted, and statements slower
than ``QUERY_SLOW_THRESHOLD_MS`` are logged with their *shape* only. Bound
parameters are never logged: a statement is normalized by replacing string and
numeric literals with ``?`` and collapsing whitespace before it reaches the log.
This keeps secrets, prompts, and user data out of the log while still showing
which query is slow.

The counters live on Flask's ``g`` for the duration of one request. Outside a
request (CLI work, background jobs) nothing is recorded. ``QUERY_PROFILING_ENABLED``
turns the instrumentation off entirely.
"""

from __future__ import annotations

import logging
import re
import time

from flask import Flask, current_app, g, has_request_context, request
from sqlalchemy import event

from app.extensions import db
from app.services.chat_audit import current_request_id

LOGGER = logging.getLogger("app.observability.query")

DEFAULT_SLOW_THRESHOLD_MS = 200.0
MAX_SHAPE_LENGTH = 200

_STRING_LITERAL = re.compile(r"'(?:''|[^'])*'")
_NUMBER_LITERAL = re.compile(r"\b\d+(?:\.\d+)?\b")


def statement_shape(statement: str) -> str:
    """Return a parameter-free, length-bounded shape of a SQL statement."""
    shape = _STRING_LITERAL.sub("?", statement or "")
    shape = _NUMBER_LITERAL.sub("?", shape)
    shape = " ".join(shape.split())
    return shape[:MAX_SHAPE_LENGTH]


def reset_query_profile() -> None:
    """Clear the current request's counters."""
    g._query_profile_count = 0
    g._query_profile_ms = 0.0


def get_query_count() -> int:
    """Return the number of SQL statements executed in the current request."""
    return int(getattr(g, "_query_profile_count", 0))


def get_query_time_ms() -> float:
    """Return the accumulated SQL execution time for the current request."""
    return float(getattr(g, "_query_profile_ms", 0.0))


def _before_cursor_execute(_conn, _cursor, _statement, _parameters, context, _executemany):
    if not has_request_context():
        return
    context._query_profile_start = time.perf_counter()


def _after_cursor_execute(_conn, _cursor, statement, _parameters, context, _executemany):
    if not has_request_context():
        return
    started = getattr(context, "_query_profile_start", None)
    if started is None:
        return
    duration_ms = (time.perf_counter() - started) * 1000

    g._query_profile_count = getattr(g, "_query_profile_count", 0) + 1
    g._query_profile_ms = getattr(g, "_query_profile_ms", 0.0) + duration_ms

    threshold = float(current_app.config.get("QUERY_SLOW_THRESHOLD_MS", DEFAULT_SLOW_THRESHOLD_MS))
    if duration_ms < threshold:
        return

    LOGGER.warning(
        "slow_query duration_ms=%.2f queries=%d method=%s endpoint=%s request_id=%s statement=%s",
        duration_ms,
        g._query_profile_count,
        request.method,
        request.url_rule.rule if request.url_rule else "unmatched",
        current_request_id() or "-",
        statement_shape(statement),
    )


def init_query_profiling(app: Flask) -> None:
    """Attach the SQLAlchemy listeners when profiling is enabled for ``app``."""
    if not bool(app.config.get("QUERY_PROFILING_ENABLED", True)):
        return

    with app.app_context():
        engine = db.engine
    event.listen(engine, "before_cursor_execute", _before_cursor_execute)
    event.listen(engine, "after_cursor_execute", _after_cursor_execute)


__all__ = [
    "get_query_count",
    "get_query_time_ms",
    "init_query_profiling",
    "reset_query_profile",
    "statement_shape",
]
