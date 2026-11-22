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
import hashlib
import threading
import time

from flask import current_app, jsonify, request
from flask_login import current_user

from app.api.error_catalog import error_response

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


def consume_details(key: str, *, max_hits: int, window: int) -> tuple[bool, int, int, int]:
    """Record a hit and return ``(allowed, remaining, retry_after, reset_after)``.

    ``remaining`` is the number of requests left in the current window,
    ``retry_after`` is the wait before a denied request may retry, and
    ``reset_after`` is the seconds until the oldest hit expires (the standard
    ``X-RateLimit-Reset`` value).
    """
    now = time.monotonic()
    with _LOCK:
        _prune(key, window)
        timestamps = _ENTRIES.setdefault(key, [])
        if len(timestamps) >= max_hits:
            oldest = timestamps[0]
            retry_after = max(round(window - (now - oldest)), 1)
            return False, 0, retry_after, retry_after
        timestamps.append(now)
        remaining = max(max_hits - len(timestamps), 0)
        reset_after = max(round(window - (now - timestamps[0])), 0)
        return True, remaining, 0, reset_after


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


def _token_subject() -> str | None:
    """Return a hashed token subject, or ``None`` when no token is supplied.

    The raw token is hashed so it is never stored as a limiter key or logged.
    """
    header = request.headers.get("Authorization", "")
    token = ""
    if header.lower().startswith("bearer "):
        token = header[7:].strip()
    if not token:
        token = request.headers.get("X-API-Token", "").strip()
    if not token:
        return None
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:24]
    return f"token:{digest}"


def _organization_subject(args, kwargs) -> str | None:
    """Resolve the organization from a header, route id, or first argument."""
    organization = request.headers.get("X-Organization-Id")
    if not organization and "workspace_id" in kwargs:
        organization = str(kwargs["workspace_id"])
    if not organization and args:
        organization = str(args[0])
    return f"org:{organization}" if organization else None


def api_quota(
    bucket: str,
    *,
    token_max_config: str = "API_QUOTA_TOKEN_MAX",
    token_window_config: str = "API_QUOTA_TOKEN_WINDOW",
    org_max_config: str = "API_QUOTA_ORG_MAX",
    org_window_config: str = "API_QUOTA_ORG_WINDOW",
):
    """Enforce per-token and per-organization quotas on a public view (#654).

    Tokens are hashed before becoming limiter keys. Responses always carry
    ``X-RateLimit-Limit``, ``X-RateLimit-Remaining``, and ``X-RateLimit-Reset``;
    a denied request gets the stable ``rate_limited`` catalog body plus
    ``Retry-After`` and is never passed to the view.
    """

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            subject = _token_subject() or f"user:{current_user.get_id()}"
            checks = []

            token_max = current_app.config.get(token_max_config) or 0
            token_window = current_app.config.get(token_window_config) or 0
            if token_max and token_window:
                checks.append((f"{bucket}:{subject}", token_max, token_window))

            organization = _organization_subject(args, kwargs)
            org_max = current_app.config.get(org_max_config) or 0
            org_window = current_app.config.get(org_window_config) or 0
            if organization and org_max and org_window:
                checks.append((f"{bucket}:{organization}", org_max, org_window))

            headers: dict[str, str] = {}
            for key, max_hits, window in checks:
                allowed, remaining, retry_after, reset_after = consume_details(
                    key, max_hits=max_hits, window=window
                )
                headers = {
                    "X-RateLimit-Limit": str(max_hits),
                    "X-RateLimit-Remaining": str(remaining),
                    "X-RateLimit-Reset": str(reset_after),
                }
                if not allowed:
                    response = error_response("rate_limited")
                    response.headers["Retry-After"] = str(retry_after)
                    response.headers.update(headers)
                    return response

            response = current_app.make_response(view(*args, **kwargs))
            for name, value in headers.items():
                response.headers.setdefault(name, value)
            return response

        return wrapper

    return decorator


def client_key(extra: str = "") -> str:
    """Build a per-client limiter key from the request's remote address."""
    from flask import request

    ip = request.headers.get("X-Forwarded-For", request.remote_addr or "unknown").split(",")[0]
    return f"{extra}:{ip}"


def ai_limit(view):
    """Per-user rate limit for the AI-powered endpoints (#28).

    Reads ``RATE_LIMIT_AI_PER_MINUTE`` (requests) and ``RATE_LIMIT_AI_WINDOW``
    (seconds) from the app config at request time, and is a no-op when
    ``RATE_LIMIT_AI_ENABLED`` is false (e.g. the testing config) or when either
    value is not configured. Over-limit requests receive the same ``429``
    ``application/json`` body as :func:`per_user_limit`.
    """

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not current_app.config.get("RATE_LIMIT_AI_ENABLED", True):
            return view(*args, **kwargs)
        max_hits = current_app.config.get("RATE_LIMIT_AI_PER_MINUTE") or 0
        window = current_app.config.get("RATE_LIMIT_AI_WINDOW") or 0
        if not max_hits or not window:
            return view(*args, **kwargs)
        allowed, retry_after = consume(
            f"ai:user:{current_user.get_id()}",
            max_hits=max_hits,
            window=window,
        )
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


def reset() -> None:
    """Clear all limiter state (used by tests)."""
    with _LOCK:
        _ENTRIES.clear()
