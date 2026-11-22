"""SIEM webhook forwarding (#629).

Security events are sent to one workspace-configured endpoint as a bounded
JSON object. Each delivery is signed with the endpoint's per-org secret using
the existing webhook contract (``<timestamp>.<raw body>``, HMAC-SHA256), and
retries use bounded exponential backoff.

Only structured facts are forwarded. Free-form details pass through
:mod:`app.services.classification`: confidential/PII fields are replaced with a
bounded redaction marker, and nested objects are never forwarded. Secrets and
prompt content therefore cannot reach the SIEM endpoint through this path.
"""

from __future__ import annotations

import re
import secrets
from datetime import UTC, datetime

import requests

from app.extensions import db
from app.models import SiemEndpoint, Workspace
from app.services import audit, job_queue, webhook_payloads, webhook_retry, webhook_signing
from app.services.classification import INTERNAL, redact_for_log
from app.services.permissions import can

SCHEMA_VERSION = 1
DEFAULT_TIMEOUT_SECONDS = 10.0

_EVENT_PATTERN = re.compile(r"^security\.[a-z0-9][a-z0-9_.-]{0,89}$")


def normalize_event_type(value: str) -> str:
    """Return a validated lowercase ``security.*`` event name."""
    event = (value or "").strip().lower()
    if not _EVENT_PATTERN.match(event):
        raise ValueError("event_type must match security.<name>.")
    return event


def _safe_details(details: dict | None) -> dict:
    """Project free-form details to flat, non-confidential scalar values."""
    if details is None:
        return {}
    if not isinstance(details, dict):
        raise ValueError("details must be a JSON object.")
    redacted = redact_for_log(details, max_level=INTERNAL)
    safe = {}
    for key, value in redacted.items():
        if value is None or isinstance(value, (str, int, float, bool)):
            safe[str(key)] = value
        else:
            safe[str(key)] = "[redacted:confidential]"
    return safe


def build_security_event(
    event_type: str,
    *,
    workspace_id: int,
    details: dict | None = None,
    actor_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    occurred_at: datetime | None = None,
) -> dict:
    """Build the structured event body sent to a SIEM endpoint."""
    moment = occurred_at or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "security-event",
        "event_type": normalize_event_type(event_type),
        "workspace_id": workspace_id,
        "occurred_at": moment.isoformat(),
        "actor_id": actor_id,
        "target_type": target_type,
        "target_id": target_id,
        "details": _safe_details(details),
    }


def get_endpoint(workspace_id: int) -> SiemEndpoint | None:
    """Return the workspace's SIEM endpoint, if configured."""
    return SiemEndpoint.query.filter_by(workspace_id=workspace_id).first()


def _workspace(workspace_id: int) -> Workspace:
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        raise LookupError("Workspace not found.")
    return workspace


def _require_manager(workspace_id: int, actor) -> None:
    if actor is not None and not can("manage_integrations", workspace_id, actor):
        raise PermissionError("You do not have permission to manage SIEM forwarding.")


def configure_endpoint(
    workspace_id: int,
    *,
    url,
    secret: str | None = None,
    is_active: bool | None = None,
    actor=None,
    commit: bool = True,
) -> tuple[SiemEndpoint, str | None]:
    """Create or update the workspace's SIEM endpoint.

    Returns ``(endpoint, raw_secret)``. ``raw_secret`` is set when a secret was
    generated or replaced, and is the only time the plaintext value is exposed.
    """
    _workspace(workspace_id)
    _require_manager(workspace_id, actor)
    try:
        validated_url = job_queue.validate_webhook_url(str(url or ""))
    except job_queue.JobQueueError as exc:
        raise ValueError(str(exc)) from exc

    provided = (secret or "").strip()
    if len(provided) > 255:
        raise ValueError("secret must be 255 characters or fewer")

    endpoint = get_endpoint(workspace_id)
    raw_secret: str | None = None
    if endpoint is None:
        raw_secret = provided or secrets.token_urlsafe(32)
        endpoint = SiemEndpoint(
            workspace_id=workspace_id,
            url=validated_url,
            is_active=True if is_active is None else bool(is_active),
        )
        endpoint.set_secret(raw_secret)
        db.session.add(endpoint)
        db.session.flush()
    else:
        endpoint.url = validated_url
        if provided:
            endpoint.set_secret(provided)
            raw_secret = provided
        if is_active is not None:
            endpoint.is_active = bool(is_active)

    audit.record(
        audit.SIEM_ENDPOINT_CONFIGURED,
        workspace_id=workspace_id,
        user=actor,
        target_type="siem_endpoint",
        target_id=endpoint.id,
        metadata={"is_active": bool(endpoint.is_active)},
    )
    if commit:
        db.session.commit()
    return endpoint, raw_secret


def delete_endpoint(workspace_id: int, *, actor=None, commit: bool = True) -> None:
    """Delete the workspace's SIEM endpoint."""
    _workspace(workspace_id)
    _require_manager(workspace_id, actor)
    endpoint = get_endpoint(workspace_id)
    if endpoint is None:
        raise LookupError("SIEM endpoint not found.")
    endpoint_id = endpoint.id
    db.session.delete(endpoint)
    audit.record(
        audit.SIEM_ENDPOINT_DELETED,
        workspace_id=workspace_id,
        user=actor,
        target_type="siem_endpoint",
        target_id=endpoint_id,
    )
    if commit:
        db.session.commit()


def forward_security_event(
    workspace_id: int,
    event_type: str,
    *,
    details: dict | None = None,
    actor_id: int | None = None,
    target_type: str | None = None,
    target_id: int | None = None,
    now: datetime | None = None,
    post=None,
    policy: webhook_retry.RetryPolicy | None = None,
    timeout: float = DEFAULT_TIMEOUT_SECONDS,
) -> dict:
    """Sign and deliver one security event with bounded retries.

    Returns a bounded result with the attempts, response code, and error label.
    The payload, secret, and response body are never part of the result.
    """
    endpoint = get_endpoint(workspace_id)
    if endpoint is None:
        return {"delivered": False, "reason": "not_configured", "attempts": []}
    if not endpoint.is_active:
        return {"delivered": False, "reason": "inactive", "attempts": []}

    event = build_security_event(
        event_type,
        workspace_id=workspace_id,
        details=details,
        actor_id=actor_id,
        target_type=target_type,
        target_id=target_id,
        occurred_at=now,
    )
    prepared, _truncated = webhook_payloads.prepare_payload(event)
    body = webhook_payloads.serialize_payload(prepared)
    moment = now or datetime.now(UTC)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=UTC)
    headers = {
        "Content-Type": "application/json",
        **webhook_signing.signature_headers(
            endpoint.signing_secret(), body, int(moment.timestamp())
        ),
    }
    sender = post or requests.post

    def send():
        return sender(
            endpoint.url,
            data=body,
            headers=headers,
            timeout=timeout,
            allow_redirects=False,
        )

    result = webhook_retry.deliver_with_retry(send, policy=policy)
    return {
        "delivered": result.success,
        "reason": None if result.success else "delivery_failed",
        "response_code": result.response_code,
        "error": result.error,
        "attempts": [attempt.to_dict() for attempt in result.attempts],
    }


__all__ = [
    "DEFAULT_TIMEOUT_SECONDS",
    "SCHEMA_VERSION",
    "build_security_event",
    "configure_endpoint",
    "delete_endpoint",
    "forward_security_event",
    "get_endpoint",
    "normalize_event_type",
]
