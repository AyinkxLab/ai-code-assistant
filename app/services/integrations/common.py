"""Shared helpers for the notification integrations.

The connectors in this package (Slack, Microsoft Teams, and email) share one
bounded contract: event payloads are small and secret-free, outbound HTTP calls
have a timeout and bounded exponential-backoff retries, errors are sanitized
before they are stored or returned, and every failure is structured with a
stable ``code``. Secrets always come from the encrypted
``integration_credentials`` store.
"""

from __future__ import annotations

import re
import time
from collections.abc import Callable

from flask import current_app

from app.extensions import db
from app.models import IntegrationCredential, Workspace
from app.services.crypto import decrypt_api_key
from app.services.permissions import can

INTEGRATIONS = ("slack", "teams", "email")

EVENT_REVIEW_COMPLETED = "review.completed"
EVENT_BUDGET_ALERT = "budget.alert"
EVENT_SECURITY_ALERT = "security.alert"
EVENT_TEST = "test"

#: Event types the connectors know how to render. Unknown types are rejected.
ALLOWED_EVENT_TYPES = (
    EVENT_REVIEW_COMPLETED,
    EVENT_BUDGET_ALERT,
    EVENT_SECURITY_ALERT,
    EVENT_TEST,
)

DEFAULT_TIMEOUT = 10.0
DEFAULT_MAX_RETRIES = 3
DEFAULT_RETRY_BASE_SECONDS = 0.5
DEFAULT_RETRY_MAX_SECONDS = 8.0
MAX_ERROR_CHARS = 500
MAX_FIELD_CHARS = 500

_URL_RE = re.compile(r"https?://\S+", re.IGNORECASE)
_WHITESPACE_RE = re.compile(r"\s+")


class IntegrationError(ValueError):
    """Base error for integration configuration and delivery failures."""

    code = "integration_error"
    http_status = 400


class IntegrationValidationError(IntegrationError):
    """The caller supplied an invalid integration setting or event."""

    code = "invalid_input"
    http_status = 400


class IntegrationNotConfigured(IntegrationError):
    """The integration has no usable credential for this workspace."""

    code = "not_configured"
    http_status = 409


class IntegrationDeliveryError(IntegrationError):
    """The remote endpoint rejected the delivery or was unreachable."""

    code = "delivery_failed"
    http_status = 502


class IntegrationPermissionError(PermissionError):
    """The caller cannot manage the workspace integration."""

    code = "permission_denied"
    http_status = 403


class IntegrationNotFound(IntegrationError):
    """The requested integration or workspace does not exist."""

    code = "not_found"
    http_status = 404


def _config(name: str, default):
    try:
        return current_app.config.get(name, default)
    except RuntimeError:  # no application context
        return default


def request_timeout() -> float:
    value = float(_config("INTEGRATION_REQUEST_TIMEOUT", DEFAULT_TIMEOUT))
    return value if value > 0 else DEFAULT_TIMEOUT


def max_retries() -> int:
    value = int(_config("INTEGRATION_MAX_RETRIES", DEFAULT_MAX_RETRIES))
    return max(0, value)


def retry_base_seconds() -> float:
    value = float(_config("INTEGRATION_RETRY_BASE_SECONDS", DEFAULT_RETRY_BASE_SECONDS))
    return max(0.0, value)


def retry_max_seconds() -> float:
    value = float(_config("INTEGRATION_RETRY_MAX_SECONDS", DEFAULT_RETRY_MAX_SECONDS))
    return max(0.0, value)


def retry_delay_seconds(attempt: int) -> float:
    """Return the capped exponential delay before retry ``attempt`` (1-based)."""
    delay = retry_base_seconds() * (2 ** max(0, attempt - 1))
    return min(delay, retry_max_seconds())


def normalize_event_type(value) -> str:
    event_type = str(value or "").strip().lower()
    if event_type not in ALLOWED_EVENT_TYPES:
        raise IntegrationValidationError(f"unsupported event type: {event_type or '<empty>'}")
    return event_type


def bounded_text(value, limit: int = MAX_FIELD_CHARS) -> str:
    """Collapse whitespace and truncate untrusted text to ``limit`` characters."""
    text = _WHITESPACE_RE.sub(" ", str(value or "")).strip()
    return text[:limit]


def _number(value):
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return value
    try:
        return float(str(value))
    except (TypeError, ValueError):
        return None


def build_event_summary(event_type: str, data: dict | None = None) -> dict:
    """Return a small, secret-free summary used by every connector.

    Only known scalar fields are copied. Strings are whitespace-collapsed and
    truncated, so a hostile event can neither inflate the payload nor smuggle a
    secret through an arbitrary key.
    """
    event_type = normalize_event_type(event_type)
    data = data if isinstance(data, dict) else {}

    if event_type == EVENT_REVIEW_COMPLETED:
        status = bounded_text(data.get("status") or "completed", 40)
        kind = bounded_text(data.get("kind") or "review", 40)
        return {
            "title": "Code review completed",
            "summary": f"Review {data.get('review_id', '')} ({kind}) is {status}.",
            "fields": {
                "kind": kind,
                "status": status,
                "review_id": bounded_text(data.get("review_id"), 40),
            },
        }
    if event_type == EVENT_BUDGET_ALERT:
        kind = bounded_text(data.get("kind") or "budget", 40)
        return {
            "title": "Budget alert",
            "summary": f"Budget threshold reached ({kind}).",
            "fields": {
                "kind": kind,
                "period": bounded_text(data.get("period"), 40),
                "spend": _number(data.get("spend")),
                "limit": _number(data.get("limit")),
                "over_by": _number(data.get("over_by")),
            },
        }
    if event_type == EVENT_SECURITY_ALERT:
        title = bounded_text(data.get("title") or "Security alert", 120)
        return {
            "title": title,
            "summary": bounded_text(
                data.get("summary") or "A security-relevant event was reported.", 400
            ),
            "fields": {
                "severity": bounded_text(data.get("severity"), 40),
                "category": bounded_text(data.get("category"), 60),
                "count": _number(data.get("count")),
                "project_id": bounded_text(data.get("project_id"), 40),
            },
        }
    return {
        "title": "Test notification",
        "summary": bounded_text(
            data.get("message") or "This is a test notification from your workspace.",
            400,
        ),
        "fields": {"test": "true"},
    }


def summary_lines(summary: dict) -> list[str]:
    """Render a summary's non-empty fields as ``Key: value`` lines."""
    lines = [summary.get("summary") or ""]
    for key, value in (summary.get("fields") or {}).items():
        if value in (None, ""):
            continue
        lines.append(f"{key.replace('_', ' ').capitalize()}: {value}")
    return [line for line in lines if line]


def retry_with_backoff(
    operation: Callable,
    *,
    retries: int | None = None,
    sleep: Callable[[float], None] | None = None,
):
    """Run ``operation`` with bounded exponential backoff.

    ``operation`` raises on retryable failures. The last exception is re-raised
    once the retry budget is exhausted, so callers keep the original cause.
    """
    attempts = max_retries() if retries is None else max(0, int(retries))
    sleeper = sleep or time.sleep
    last_error: Exception | None = None
    for attempt in range(attempts + 1):
        try:
            return operation()
        except Exception as exc:
            last_error = exc
            if attempt >= attempts:
                raise
            sleeper(retry_delay_seconds(attempt + 1))
    raise last_error  # pragma: no cover - loop always returns or raises


def sanitize_error(error) -> str:
    """Return a bounded, secret-free error label.

    URLs are replaced because Slack/Teams webhook URLs carry their own secret
    token in the path, and any ``key=value``-style secret would be visible in a
    raw exception message.
    """
    if isinstance(error, BaseException):
        text = f"{type(error).__name__}: {error}"
    else:
        text = str(error or "")
    text = _URL_RE.sub("[redacted-url]", text)
    text = _WHITESPACE_RE.sub(" ", text).strip()
    return text[:MAX_ERROR_CHARS] or "integration call failed"


def require_owner(workspace_id: int, user) -> Workspace:
    """Return the workspace when ``user`` may manage integrations, else raise."""
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        raise IntegrationNotFound("Workspace not found.")
    if not can("manage_integrations", workspace_id, user):
        raise IntegrationPermissionError(
            "You do not have permission to manage workspace integrations."
        )
    return workspace


def load_credential_value(workspace_id: int, integration: str, name: str) -> str:
    """Decrypt a credential for delivery without a user context.

    This is the internal, delivery-only path used by event dispatch and the
    signed inbound-email endpoint: authorization is either the route-level owner
    check (test sends) or the HMAC signature (inbound mail), never an assumption
    that a caller is an owner. The plaintext stays in memory.
    """
    credential = IntegrationCredential.query.filter_by(
        workspace_id=workspace_id,
        integration=integration,
        name=name,
    ).first()
    if credential is None:
        raise IntegrationNotConfigured(f"{integration} is not configured for this workspace.")
    try:
        return decrypt_api_key(credential.encrypted_value)
    except ValueError as exc:
        raise IntegrationNotConfigured(
            f"The stored {integration} credential cannot be decrypted."
        ) from exc


def credential_names(workspace_id: int, integration: str) -> set[str]:
    """Return the credential names stored for one workspace integration."""
    rows = IntegrationCredential.query.filter_by(
        workspace_id=workspace_id,
        integration=integration,
    ).all()
    return {row.name for row in rows}


def is_configured(workspace_id: int, integration: str) -> bool:
    names = credential_names(workspace_id, integration)
    if integration in ("slack", "teams"):
        return "webhook_url" in names
    if integration == "email":
        return bool(names & {"inbound_secret", "reply_to"})
    return False


__all__ = [
    "ALLOWED_EVENT_TYPES",
    "EVENT_BUDGET_ALERT",
    "EVENT_REVIEW_COMPLETED",
    "EVENT_SECURITY_ALERT",
    "EVENT_TEST",
    "INTEGRATIONS",
    "IntegrationDeliveryError",
    "IntegrationError",
    "IntegrationNotConfigured",
    "IntegrationNotFound",
    "IntegrationPermissionError",
    "IntegrationValidationError",
    "bounded_text",
    "build_event_summary",
    "credential_names",
    "is_configured",
    "load_credential_value",
    "max_retries",
    "normalize_event_type",
    "request_timeout",
    "require_owner",
    "retry_delay_seconds",
    "retry_with_backoff",
    "sanitize_error",
    "summary_lines",
]
