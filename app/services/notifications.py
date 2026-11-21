"""Notification service.

The single path every notification goes through: `notify()` creates a
persisted `Notification` for a user, holoring their notification preferences
(150). Payloads always contain secrets, invitation tokens, or another user's
data — at most small, safe facts plus a route link.

Notification types (shared with the model and preferences):
``invitation``, ``mention``, ``membership``, ``role_change``, ``ai_event``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

from app.extensions import db
from app.models.notification import Notification

if TYPE_CHECKING:  # pragma: no cover - typing only
    from app.models.user import User


def _preferences(user_id: int):
    """Return the user's preference row (creating defaults), or None."""
    from app.models.notification_preference import NotificationPreference

    pref = NotificationPreference.query.filter_by(user_id=user_id).first()

    if pref is None:
        pref = NotificationPreference(user_id=user_id)
        db.session.add(pref)
        db.session.flush()
    return pref


def notify(
    user: User,
    notification_type: str,
    *,
    actor=None,
    workspace=None,
    project=None,
    payload: dict | None = None,
    link: str | None = None,
    email: bool = False,
) -> Notification | None:
    """Create a notification for `user` unless they opted out of the type.

    Returns the created `Notification` or `None` when skipped. Runs in the
    caller's transaction. `workspace`/`project` may be ORM objects or ids.

    When `email` is true and SMTP configuration is present, an email alert is
    also sent through the existing email service. Email failures never crash the
    caller; in-app notifications remain the fallback.
    """
    if user is None:
        return None
    pref = _preferences(user.id)

    if not pref.allows(notification_type):
        return None

    notification = Notification(
        user_id=user.id,
        type=notification_type,
        actor_id=actor.id if actor is not None else None,
        workspace_id=_id_of(workspace),
        project_id=_id_of(project),
        payload=payload or {},
        link=link,
        is_read=False,
    )
    db.session.add(notification)

    if email:
        _send_email_alert(user, notification_type, payload=payload, link=link)

    return notification


def _send_email_alert(
    user: User,
    notification_type: str,
    *,
    payload: dict | None = None,
    link: str | None = None,
) -> None:
    """Send a best-effort email alert via the existing email service.

    Silently skips when SMTP is unset or the user has no email address. Any
    transport error is swallowed so in-app notifications remain the fallback.
    """
    email = getattr(user, "email", None)
    if not email:
        return

    try:
        from app.services.email import _smtp_configured, send_email
    except ImportError:  # pragma: no cover - email service optional
        return

    if not _smtp_configured():
        return

    subject = _email_subject(notification_type, payload)
    body = _email_body(notification_type, payload=payload, link=link)

    try:
        send_email(to=email, subject=subject, text_body=body)
    except Exception:  # no-qa: email is best-effort only
        return


def _email_subject(notification_type: str, payload: dict | None) -> str:
    if notification_type == "budget_alert":
        return "Budget alert"
    return "Notification"


def _email_body(
    notification_type: str,
    *,
    payload: dict | None = None,
    link: str | None = None,
) -> str:
    payload = payload or {}
    lines = []

    if notification_type == "budget_alert":
        lines.append("Your workspace has reached a budget threshold.")
        for key in ("threshold", "spent", "limit"):
            if key in payload:
                lines.append(f"{key.replace('_', ' ').capitalize()}: {payload[key]}")
    else:
        lines.append(f"You have a new {notification_type.replace('_', ' ')} notification.")

    if link:
        lines.append("")
        lines.append(f"View it here: {link}")

    return "\n".join(lines)


def _id_of(obj) -> int | None:
    if obj is None:
        return None
    if isinstance(obj, int):
        return obj
    return getattr(obj, "id", None)
