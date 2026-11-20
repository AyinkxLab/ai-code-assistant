"""The /metrics endpoint (admin session or bearer token)."""

from __future__ import annotations

import hmac

from flask import Blueprint, Response, current_app, request
from flask_login import current_user

from .metrics import CONTENT_TYPE, render_metrics

bp = Blueprint("observability", __name__)


def _bearer_token() -> str | None:
    """Return the presented bearer token, or ``None`` when absent/malformed."""
    header = request.headers.get("Authorization", "")
    scheme, separator, value = header.partition(" ")
    if not separator or scheme.lower() != "bearer":
        return None
    token = value.strip()
    return token or None


def _is_admin_session() -> bool:
    """Return True for an authenticated site-admin session.

    Bare Flask apps used by unit tests have no login manager; they simply fail
    closed instead of raising through the proxy.
    """
    if not hasattr(current_app, "login_manager"):
        return False
    return bool(
        getattr(current_user, "is_authenticated", False)
        and getattr(current_user, "is_admin", False)
    )


def _authorized() -> tuple[bool, bool]:
    """Return ``(allowed, used_token)`` for the current request."""
    configured = current_app.config.get("METRICS_TOKEN") or ""
    configured = configured.strip() if isinstance(configured, str) else ""
    if configured:
        presented = _bearer_token()
        allowed = presented is not None and hmac.compare_digest(presented, configured)
        return allowed, True
    return _is_admin_session(), False


@bp.get("/metrics")
def metrics() -> Response:
    if not current_app.config.get("METRICS_ENABLED", True):
        return Response("not found\n", status=404, mimetype="text/plain")

    allowed, used_token = _authorized()
    if not allowed:
        response = Response(
            "metrics authentication required\n",
            status=401,
            mimetype="text/plain",
        )
        response.headers["Cache-Control"] = "no-store"
        if used_token:
            response.headers["WWW-Authenticate"] = 'Bearer realm="metrics"'
        return response

    # Queue depth is read from the database at scrape time so a stalled worker
    # does not freeze the last value. A database error must not hide the other
    # metrics, so this refresh is best effort and logged.
    try:
        from app.services.job_queue import refresh_queue_metrics

        refresh_queue_metrics()
    except Exception:  # pragma: no cover - depends on a live database
        current_app.logger.debug("queue metrics refresh failed", exc_info=True)

    response = Response(render_metrics(), content_type=CONTENT_TYPE)
    response.headers["Cache-Control"] = "no-store"
    return response
