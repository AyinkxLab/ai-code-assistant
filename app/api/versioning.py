"""API versioning and deprecation policy (#651).

Stable resources live under the ``/api/v1`` path prefix. Every response below
``/api/`` also carries ``X-API-Version`` so clients can detect the contract
version without parsing the path. Deprecated views add the standard
``Deprecation: true`` and ``Sunset`` headers (and an optional ``Link`` to the
successor) via :func:`deprecated`.
"""

from __future__ import annotations

from functools import wraps

from flask import make_response, request

API_VERSION = "1"
API_VERSION_HEADER = "X-API-Version"
SUPPORTED_VERSIONS = ("1",)
DEPRECATION_HEADER = "Deprecation"
SUNSET_HEADER = "Sunset"


def init_versioning(app) -> None:
    """Install the API version response header on every API response."""

    @app.after_request
    def add_api_version_header(response):
        if request.path == "/api" or request.path.startswith("/api/"):
            response.headers.setdefault(API_VERSION_HEADER, API_VERSION)
        return response


def apply_deprecation(response, *, sunset: str, replacement: str | None = None):
    """Mark a response as deprecated with the standard headers."""
    response.headers[DEPRECATION_HEADER] = "true"
    response.headers[SUNSET_HEADER] = sunset
    if replacement:
        link = f'<{replacement}>; rel="successor-version"'
        existing = response.headers.get("Link")
        response.headers["Link"] = f"{existing}, {link}" if existing else link
    return response


def deprecated(*, sunset: str, replacement: str | None = None):
    """Decorator that adds deprecation headers to a view's response."""

    def decorator(view):
        @wraps(view)
        def wrapper(*args, **kwargs):
            response = make_response(view(*args, **kwargs))
            return apply_deprecation(response, sunset=sunset, replacement=replacement)

        return wrapper

    return decorator
