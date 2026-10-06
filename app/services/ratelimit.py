"""Small in-memory sliding-window rate limiter.

Used for the public collaboration endpoints (invitation accept/decline/landing)
and the presence heartbeat, and — via :func:`per_user_limit` — for the costly
per-user Phase 5 endpoints (project import, search, chat/stream, analysis, #106).
Limits are per-key (typically per client IP, or per user id for the endpoint
limiter) over a configurable window. The limiter is process-local, which is
acceptable for the default single-worker deployments and CI; multi-worker
deployments should back it with a shared store (out of scope here).
"""

from __future__ import annotations

import functools
import threading
import time
from datetime import UTC, datetime, timedelta

from flask import current_app, jsonify
from flask_login import current_user

_ENTRIES: dict[str, list[float]] = {}
_LOCK = threading.Lock()
_DAILY_LOCK = threading.Lock()


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
    """

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            max_hits = current_app.config.get(max_config) or 0
            window = current_app.config.get(window_config) or 0
            key = f"{bucket}:user:{current_user.get_id()}"
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


# --------------------------------------------------------------------------- #
# Persistent daily counters
# --------------------------------------------------------------------------- #
# The sliding window above is process-local; a daily cap must survive restarts
# and be shared by every worker, so it is backed by the ``rate_limits`` table.
# An in-memory dict is kept as a fallback for when the database is unavailable
# (e.g. before migrations have run), so chat never hard-fails on the limiter.

_DAILY_ENTRIES: dict[str, int] = {}


def _daily_key(key: str) -> str:
    """Bucket key for ``key`` scoped to the current UTC day."""
    return f"{key}:{datetime.now(UTC).date().isoformat()}"


def _seconds_until_utc_midnight(now: datetime | None = None) -> int:
    now = now or datetime.now(UTC)
    tomorrow = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0, microsecond=0)
    return max(round((tomorrow - now).total_seconds()), 1)


def _daily_count(key: str) -> int:
    """Return the persisted daily hit count for ``key`` (today, UTC)."""
    daily_key = _daily_key(key)
    try:
        from app.models import RateLimit

        row = RateLimit.query.filter_by(key=daily_key).first()
        return row.hits if row is not None else 0
    except Exception:  # pragma: no cover - database unavailable
        return _DAILY_ENTRIES.get(daily_key, 0)


def daily_count(key: str) -> int:
    """Return the persisted daily hit count for ``key``."""
    return _daily_count(key)


def daily_remaining(key: str, *, max_hits: int) -> int:
    """Return how many daily hits remain for ``key`` (never negative)."""
    return max(max_hits - _daily_count(key), 0)


def daily_consume(key: str, *, max_hits: int) -> tuple[bool, int]:
    """Record a hit against the persistent daily cap for ``key``.

    Returns ``(allowed, retry_after_seconds)``. ``retry_after`` is the number of
    seconds until the counter resets (the next UTC midnight) when the caller is
    over ``max_hits``, and ``0`` otherwise. A ``max_hits`` of ``0`` or less
    disables the cap.
    """
    if max_hits <= 0:
        return True, 0
    daily_key = _daily_key(key)
    now = datetime.now(UTC)
    try:
        from app.extensions import db
        from app.models import RateLimit

        row = RateLimit.query.filter_by(key=daily_key).first()
        if row is None:
            row = RateLimit(key=daily_key, window_start=now, hits=1)
            db.session.add(row)
            db.session.commit()
            return True, 0
        if (row.hits or 0) >= max_hits:
            return False, _seconds_until_utc_midnight(now)
        row.hits = (row.hits or 0) + 1
        db.session.commit()
        return True, 0
    except Exception:  # pragma: no cover - database unavailable
        try:
            from app.extensions import db

            db.session.rollback()
        except Exception:
            pass
        current = _DAILY_ENTRIES.get(daily_key, 0)
        if current >= max_hits:
            return False, _seconds_until_utc_midnight(now)
        _DAILY_ENTRIES[daily_key] = current + 1
        return True, 0


def peek(
    key: str,
    *,
    max_hits: int,
    window: int,
    daily_cap: int = 0,
) -> tuple[int, int]:
    """Report ``(remaining, reset_after)`` without recording a hit.

    ``remaining`` is the smaller of the sliding-window and daily budgets so the
    UI can warn the user before the stricter of the two is exhausted.
    """
    remaining = max(max_hits - count(key, window=window), 0)
    reset_after = retry_after(key, window=window)
    if daily_cap > 0:
        daily = daily_remaining(key, max_hits=daily_cap)
        if daily < remaining:
            remaining = daily
            reset_after = _seconds_until_utc_midnight()
    return remaining, reset_after


def peek_daily(key: str, *, daily_cap: int) -> tuple[int, int]:
    """Report ``(daily_remaining, seconds_until_reset)`` without recording a hit."""
    if daily_cap <= 0:
        return 0, 0
    return daily_remaining(key, max_hits=daily_cap), _seconds_until_utc_midnight()


def reset_daily() -> None:
    """Clear persisted + in-memory daily counters (used by tests)."""
    with _DAILY_LOCK:
        _DAILY_ENTRIES.clear()


def reset() -> None:
    """Clear all in-memory limiter state (used by tests)."""
    with _LOCK:
        _ENTRIES.clear()
    reset_daily()
