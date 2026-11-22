"""Structured, security-relevant logging for the chat feature (issue #17).

Emits machine-readable JSON records for logins, API-key lifecycle events, and
provider calls. Two rules are enforced by construction:

* **No PII or secrets.** Callers only pass identifiers and safe metadata
  (provider name, status, latency, token counts, retry counts); message content
  and API-key material are never accepted or serialized.
* **Correlation.** Every record carries the request id set by the middleware in
  :func:`init_app`, so one request can be followed across log lines (the same id
  is echoed in the ``X-Request-ID`` response header).

Logging is configurable: ``CHAT_AUDIT_LOG_ENABLED`` turns it off entirely and
``CHAT_AUDIT_LOG_LEVEL`` (DEBUG/INFO/…/OFF) sets the threshold.
"""

from __future__ import annotations

import json
import logging
import re
import uuid
from datetime import UTC, datetime

from flask import g, request

from app.services.classification import redact_for_log

#: Correlation header echoed on responses and read from inbound requests.
REQUEST_ID_HEADER = "X-Request-ID"

LOGGER = logging.getLogger("app.chat_audit")

_REQUEST_ID_RE = re.compile(r"[^A-Za-z0-9._:-]")


class JsonFormatter(logging.Formatter):
    """Render an audit record as a single-line JSON object."""

    def format(self, record: logging.LogRecord) -> str:
        fields = dict(getattr(record, "audit_fields", {}) or {})
        payload: dict = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "event": fields.pop("event", record.getMessage()),
        }
        payload.update(fields)
        return json.dumps(payload, default=str, separators=(",", ":"))


def _sanitize_request_id(value: str | None) -> str | None:
    """Keep only safe characters and bound the length, or return ``None``."""
    if not value:
        return None
    cleaned = _REQUEST_ID_RE.sub("", value.strip())[:64]
    return cleaned or None


def current_request_id() -> str | None:
    """The current request's correlation id, or ``None`` outside a request."""
    try:
        return getattr(g, "request_id", None)
    except RuntimeError:
        return None


def log_event(
    event: str,
    *,
    user_id: int | None = None,
    level: int = logging.INFO,
    **fields: object,
) -> None:
    """Emit one structured audit record.

    ``fields`` must contain only safe metadata (never message content or key
    material). The current request id and ``event`` are added automatically.
    """
    if LOGGER.disabled:
        return
    payload = {"event": event, "user_id": user_id, "request_id": current_request_id()}
    payload.update(redact_for_log(fields))
    LOGGER.log(level, event, extra={"audit_fields": payload})


def init_app(app) -> None:
    """Install request-id middleware and configure the JSON audit logger."""
    _register_request_id(app)

    enabled = bool(app.config.get("CHAT_AUDIT_LOG_ENABLED", True))
    level_name = str(app.config.get("CHAT_AUDIT_LOG_LEVEL", "INFO")).strip().upper()
    if not enabled or level_name in {"OFF", "DISABLED", "NONE", "0"}:
        LOGGER.disabled = True
        return

    LOGGER.disabled = False
    LOGGER.setLevel(getattr(logging, level_name, logging.INFO))
    LOGGER.propagate = False
    if not any(getattr(handler, "_chat_audit", False) for handler in LOGGER.handlers):
        handler = logging.StreamHandler()
        handler.setFormatter(JsonFormatter())
        handler._chat_audit = True  # type: ignore[attr-defined]
        LOGGER.addHandler(handler)


def _register_request_id(app) -> None:
    @app.before_request
    def _assign_request_id() -> None:
        g.request_id = (
            _sanitize_request_id(request.headers.get(REQUEST_ID_HEADER)) or uuid.uuid4().hex
        )

    @app.after_request
    def _expose_request_id(response):
        request_id = getattr(g, "request_id", None)
        if request_id:
            response.headers[REQUEST_ID_HEADER] = request_id
        return response
