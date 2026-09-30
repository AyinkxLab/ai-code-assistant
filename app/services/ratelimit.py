"""Small in-memory sliding-window rate limiter.

Used for the public collaboration endpoints (invitation accept/decline/landing)
and the presence heartbeat, and — via :func:`per_user_limit` — for the costly
per-user Phase 5 endpoints (project import, search, chat/stream, analysis, #106).
Limits are per-key (typically per client IP, or per user id for the endpoint
limiter) over a configurable window. In addition to the short sliding window,
:func:`per_user_limit` supports an optional persistent daily cap backed by the
existing Postgres store (see :mod:`app.services.ratelimit_store`), so that
multi-worker deployments share a durable per-user budget. The in-memory window
remains process-local, which is acceptable for the default single-worker
deployments and CI.
"""

from __future__ import annotations

import functools
import threading
import time

from flask import current_app, jsonify
from flask_login import current_user

from app.services import ratelimit_store

_ENTRIES: dict[str, list[float]] = {}
_LOCK = threading.Lock()


def _prune(key: str, window: int) -> None:
    cutoff = time.monotonic() - window
    timestamps = _ENTRIES.get(key)
    if not timestamps:
        return
    kept = [ts for ts in timestamps if ts > cutoff]
    if kept:
        _ENTRIES[key] = kept
    else:
        _ENTRIES.pop(key, None)


def hit(key: str, *, max_hits: int | None = None, window: int | None = None) -> bool:
    """Record a hit for ``key`` and return ``True`` when still within the limit.

    When the limit is exceeded the hit is still recorded, so repeated abuse
    keeps the key hot.
    """
    if max_hits is None:
        max_hits = current_app.config.get("RATE_LIMIT_MAX", 30)
    if window is None:
        window = current_app.config.get("RATE_LIMIT_WINDOW_SECONDS", 300)

    now = time.monotonic()
    with _LOCK:
        _prune(key, window)
        timestamps = _ENTRIES.setdefault(key, [])
        allowed = len(timestamps) < max_hits
        timestamps.append(now)
        return allowed


def consume(key: str, *, max_hits: int, window: int) -> tuple[bool, int]:
    """Record a hit for ``key`` and return ``(allowed, retry_after_seconds)``.

    Unlike :func:`hit`, this reports how long the caller must wait before the
    limit resets (the time remaining on the oldest hit in the window). It is the
    building block for the per-user endpoint limiter below.
    """
    now = time.monotonic()
    with _LOCK:
        _prune(key, window)
        timestamps = _ENTRIES.setdefault(key, [])
        if len(timestamps) >= max_hits:
            oldest = timestamps[0]
            retry_after = max(round(window - (now - oldest)), 1)
            return False, retry_after
        timestamps.append(now)
        return True, 0


def count(key: str, *, window: int) -> int:
    """Return the current number of recorded hits for ``key`` in ``window``.

    Read-only: unlike :func:`hit`/:func:`consume` it never records a hit, so it
    is safe to call as a pre-check before deciding whether an action is allowed.
    """
    with _LOCK:
        _prune(key, window)
        return len(_ENTRIES.get(key, ()))


def record(key: str) -> None:
    """Record a hit for ``key`` outside of any limit check.

    Used for failure-based throttling (e.g. the OAuth callback): only failed
    attempts are recorded, so legitimate successes never count against the limit.
    """
    with _LOCK:
        _ENTRIES.setdefault(key, []).append(time.monotonic())


def retry_after(key: str, *, window: int) -> int:
    """Return seconds until the oldest recorded hit for ``key`` expires."""
    with _LOCK:
        _prune(key, window)
        timestamps = _ENTRIES.get(key)
        if not timestamps:
            return 0
        return max(round(window - (time.monotonic() - timestamps[0])), 1)


def clear(key: str) -> None:
    """Drop all recorded hits for a single ``key`` (e.g. after a success)."""
    with _LOCK:
        _ENTRIES.pop(key, None)


def per_user_limit(bucket: str, *, max_config: str, window_config: str):
    """Decorator enforcing a per-user sliding-window limit on a view.

    The maximum number of requests and the window length (in seconds) are read
    from ``max_config`` / ``window_config`` on the app config at request time,
    so they remain environment-configurable. When the limit is exceeded the
    wrapped view is not called and a ``429`` JSON response carrying a
    ``Retry-After`` header is returned instead.

    A persistent daily cap may be configured via ``<BUCKET>_DAILY_MAX`` (or the
    generic ``RATE_LIMIT_DAILY_MAX``) and ``RATE_LIMIT_DAILY_WINDOW_SECONDS``.
    When set, the daily budget is checked against the shared store before the
    in-memory window so that the cap is enforced across workers and survives
    restarts.
    """

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            max_hits = current_app.config.get(max_config) or 0
            window = current_app.config.get(window_config) or 0
            user_id = current_user.get_id()
            key = f"{bucket}:user:{user_id}"

            daily_max = (
                current_app.config.get(f"{bucket.upper()}_DAILY_MAX")
                or current_app.config.get("RATE_LIMIT_DAILY_MAX")
                or 0
            )
            if daily_max:
                daily_window = current_app.config.get(
                    "RATE_LIMIT_DAILY_WINDOW_SECONDS", 86400
                )
                allowed, retry_after = ratelimit_store.consume_daily(
                    bucket=bucket,
                    user_id=user_id,
                    max_hits=daily_max,
                    window=daily_window,
                )
                if not allowed:
                    response = jsonify(
                        {
                            "error": "Daily rate limit exceeded. Please retry later.",
                            "kind": "rate_limited",
                        }
                    )
                    response.status_code = 429
                    response.headers["Retry-After"] = str(retry_after)
                    return response

            allowed, retry_after = consume(key, max_hits=max_hits, window=window)
            if not allowed:
                response = jsonify(
                    {
                        "error": "Rate limit exceeded. Please retry later.",
                        "kind": "rate_limited",
                    }
                )
                response.status_code = 429
                response.headers["Retry-After"] = str(retry_after)
                return response
            return view(*args, **kwargs)

        return wrapper

    return decorator


def client_key(extra: str = "") -> str:
    """Build a per-client limiter key from the request's remote address."""
    from flask import request

    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0]
    return f"{extra}:{ip}"


def reset() -> None:
    """Clear all limiter state (used by tests)."""
    with _LOCK:
        _ENTRIES.clear()
    ratelimit_store.reset()
