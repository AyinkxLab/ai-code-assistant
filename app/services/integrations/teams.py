"""Microsoft Teams incoming-webhook notifications (#679).

The connector posts a bounded MessageCard to a Teams incoming webhook. The
webhook URL is stored encrypted as ``integration=teams, name=webhook_url`` and
is validated with the same SSRF guard used by the webhook job queue: public
HTTP(S) target, no embedded credentials, no loopback/private addresses. Delivery
retries transient failures (network errors, HTTP 429, HTTP 5xx) with bounded
exponential backoff.
"""

from __future__ import annotations

import contextlib

import requests

from app.services import job_queue
from app.services.integration_credentials import (
    IntegrationCredentialError,
    delete_credential,
    set_credential,
)
from app.services.integrations import health
from app.services.integrations.common import (
    EVENT_TEST,
    IntegrationDeliveryError,
    IntegrationError,
    IntegrationNotConfigured,
    IntegrationValidationError,
    build_event_summary,
    load_credential_value,
    normalize_event_type,
    request_timeout,
    retry_with_backoff,
    sanitize_error,
    summary_lines,
)

INTEGRATION = "teams"
WEBHOOK_CREDENTIAL = "webhook_url"
MAX_TEXT_CHARS = 4000
CARD_CONTEXT = "https://schema.org/extensions"


def configure(workspace_id: int, user, webhook_url) -> dict:
    """Validate and store the Teams webhook URL (owner only)."""
    try:
        url = job_queue.validate_webhook_url(str(webhook_url or ""))
    except job_queue.JobQueueError as exc:
        raise IntegrationValidationError(str(exc)) from exc
    try:
        set_credential(workspace_id, INTEGRATION, WEBHOOK_CREDENTIAL, url, user=user)
    except IntegrationCredentialError as exc:
        raise IntegrationValidationError(str(exc)) from exc
    return {"integration": INTEGRATION, "configured": True}


def delete_configuration(workspace_id: int, user) -> None:
    with contextlib.suppress(IntegrationCredentialError):
        delete_credential(workspace_id, INTEGRATION, WEBHOOK_CREDENTIAL, user=user)


def build_payload(summary: dict) -> dict:
    """Render a bounded Teams MessageCard from a summary."""
    title = str(summary.get("title") or "Notification")[:200]
    lines = summary_lines(summary)
    text = "\n\n".join(lines)[:MAX_TEXT_CHARS]
    return {
        "@type": "MessageCard",
        "@context": CARD_CONTEXT,
        "summary": title,
        "title": title,
        "text": text,
    }


def _send(workspace, event_type: str, data: dict | None, *, post=None, sleep=None) -> dict:
    event_type = normalize_event_type(event_type)
    summary = build_event_summary(event_type, data)
    payload = build_payload(summary)
    try:
        url = load_credential_value(workspace.id, INTEGRATION, WEBHOOK_CREDENTIAL)
    except IntegrationNotConfigured:
        raise

    poster = post or requests.post

    def attempt():
        response = poster(
            url,
            json=payload,
            timeout=request_timeout(),
            allow_redirects=False,
        )
        code = int(getattr(response, "status_code", 0))
        if code == 429 or code >= 500:
            exc = IntegrationDeliveryError(f"Teams returned HTTP {code}")
            exc.status_code = code
            raise exc
        return response

    try:
        response = retry_with_backoff(attempt, sleep=sleep)
        code = int(getattr(response, "status_code", 0))
        if code >= 400:
            raise IntegrationDeliveryError(f"Teams returned HTTP {code}")
    except IntegrationError as exc:
        health.record_error(
            workspace.id,
            INTEGRATION,
            exc,
            code=exc.code,
            status_code=getattr(exc, "status_code", None),
        )
        raise
    except Exception as exc:
        health.record_error(workspace.id, INTEGRATION, exc)
        raise IntegrationDeliveryError(sanitize_error(exc)) from exc

    health.record_success(workspace.id, INTEGRATION, status_code=code)
    return {
        "status": "sent",
        "integration": INTEGRATION,
        "event_type": event_type,
        "status_code": code,
    }


def send_event(
    workspace, event_type: str, data: dict | None = None, *, post=None, sleep=None
) -> dict:
    """Send one event to the workspace's Teams webhook."""
    return _send(workspace, event_type, data, post=post, sleep=sleep)


def test_connection(workspace, *, post=None, sleep=None) -> dict:
    """Send a test MessageCard and record the outcome in integration health."""
    return _send(
        workspace,
        EVENT_TEST,
        {"message": "Test notification from the AI Code Assistant."},
        post=post,
        sleep=sleep,
    )


__all__ = [
    "INTEGRATION",
    "WEBHOOK_CREDENTIAL",
    "build_payload",
    "configure",
    "delete_configuration",
    "send_event",
    "test_connection",
]
