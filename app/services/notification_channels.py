"""Notification channel preferences and test sends (#631).

Validation is centralized here so the API returns structured errors and the
stored matrix can never contain an unknown event type or channel.
"""

from __future__ import annotations

from app.extensions import db
from app.models.notification_channel import (
    CHANNELS,
    DEFAULT_CHANNELS,
    EVENT_TYPES,
    NotificationChannelPreference,
)


class NotificationChannelError(ValueError):
    """Raised for invalid event types, channels, or channel configuration."""


def _rows_for(workspace_id: int) -> dict[str, NotificationChannelPreference]:
    rows = NotificationChannelPreference.query.filter_by(workspace_id=workspace_id).all()
    return {row.event_type: row for row in rows}


def get_matrix(workspace_id: int) -> dict:
    """Return the full event-type x channel matrix with defaults applied."""
    rows = _rows_for(workspace_id)
    return {
        "workspace_id": workspace_id,
        "channels": list(CHANNELS),
        "rows": [
            (
                rows[event_type].to_dict()
                if event_type in rows
                else {"event_type": event_type, **DEFAULT_CHANNELS}
            )
            for event_type in EVENT_TYPES
        ],
    }


def _validate_row(row: object) -> tuple[str, dict[str, bool]]:
    if not isinstance(row, dict):
        raise NotificationChannelError("each row must be an object")
    event_type = str(row.get("event_type") or "").strip()
    if event_type not in EVENT_TYPES:
        raise NotificationChannelError(f"unknown event type: {event_type or '<empty>'}")
    channels: dict[str, bool] = {}
    for channel in CHANNELS:
        if channel not in row:
            raise NotificationChannelError(f"missing channel: {channel}")
        value = row[channel]
        if not isinstance(value, bool):
            raise NotificationChannelError(f"channel {channel} must be a boolean")
        channels[channel] = value
    unknown = set(row) - {"event_type", *CHANNELS}
    if unknown:
        raise NotificationChannelError(f"unknown channel keys: {', '.join(sorted(unknown))}")
    return event_type, channels


def update_matrix(workspace_id: int, user, rows: object) -> dict:
    """Validate and persist a full preference matrix."""
    if not isinstance(rows, list):
        raise NotificationChannelError("rows must be a list")
    parsed: dict[str, dict[str, bool]] = {}
    for row in rows:
        event_type, channels = _validate_row(row)
        if event_type in parsed:
            raise NotificationChannelError(f"duplicate event type: {event_type}")
        parsed[event_type] = channels
    if not parsed:
        raise NotificationChannelError("at least one event type is required")

    existing = _rows_for(workspace_id)
    for event_type, channels in parsed.items():
        row = existing.get(event_type)
        if row is None:
            row = NotificationChannelPreference(workspace_id=workspace_id, event_type=event_type)
            db.session.add(row)
        row.email = channels["email"]
        row.chat = channels["chat"]
        row.webhook = channels["webhook"]
        row.updated_by_id = getattr(user, "id", None)
    db.session.commit()
    return get_matrix(workspace_id)


def send_test(
    workspace,
    user,
    *,
    event_type: str,
    channel: str,
    webhook_url: str | None = None,
) -> dict:
    """Send a test notification through one channel."""
    if event_type not in EVENT_TYPES:
        raise NotificationChannelError(f"unknown event type: {event_type}")
    if channel not in CHANNELS:
        raise NotificationChannelError(f"unknown channel: {channel}")

    if channel == "chat":
        from app.services import notifications as notifications_service

        notifications_service.notify(
            user,
            "ai_event",
            workspace=workspace,
            payload={"test": True, "event_type": event_type, "channel": "chat"},
            link="/workspaces/notifications",
        )
        db.session.commit()
        return {"status": "queued", "channel": "chat", "event_type": event_type}

    if channel == "email":
        from app.services import email as email_service

        if not email_service._smtp_configured() or not getattr(user, "email", None):
            return {
                "status": "skipped",
                "channel": "email",
                "reason": "email is not configured",
            }
        delivered = email_service.send_email(
            to=user.email,
            subject="Test notification",
            text_body=f"Test notification for {event_type} from your workspace.",
        )
        if not delivered:
            return {
                "status": "skipped",
                "channel": "email",
                "reason": "email delivery failed",
            }
        return {"status": "sent", "channel": "email", "event_type": event_type}

    from app.services import job_queue

    if not webhook_url:
        return {
            "status": "skipped",
            "channel": "webhook",
            "reason": "a webhook URL is required",
        }
    url = job_queue.validate_webhook_url(webhook_url)
    try:
        job = job_queue.enqueue(
            "webhook.deliver",
            {"url": url, "payload": {"test": True, "event_type": event_type}},
        )
    except job_queue.JobQueueError as exc:
        return {"status": "skipped", "channel": "webhook", "reason": str(exc)}
    return {
        "status": "queued",
        "channel": "webhook",
        "event_type": event_type,
        "job_id": job.id,
    }


__all__ = [
    "NotificationChannelError",
    "get_matrix",
    "send_test",
    "update_matrix",
]
