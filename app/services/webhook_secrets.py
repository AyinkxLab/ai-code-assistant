"""Webhook signing-secret rotation (#673).

A rotation installs a new secret and keeps the previous one valid for a short
window so requests already signed with it can still be verified. The window is
bounded to 24 hours.
"""

from __future__ import annotations

from datetime import datetime

from app.models.webhook import WebhookSubscription

DEFAULT_ROTATION_WINDOW_SECONDS = 300
MAX_ROTATION_WINDOW_SECONDS = 86_400


def rotate_secret(
    subscription: WebhookSubscription,
    new_secret: str,
    *,
    window_seconds: int = DEFAULT_ROTATION_WINDOW_SECONDS,
    now: datetime | None = None,
) -> WebhookSubscription:
    """Rotate ``subscription``'s secret and return it."""
    if not isinstance(subscription, WebhookSubscription):
        raise ValueError("subscription must be a WebhookSubscription")
    if isinstance(window_seconds, bool) or not isinstance(window_seconds, int):
        raise ValueError("window_seconds must be an integer")
    if not 0 <= window_seconds <= MAX_ROTATION_WINDOW_SECONDS:
        raise ValueError(f"window_seconds must be between 0 and {MAX_ROTATION_WINDOW_SECONDS}")
    subscription.rotate_secret(new_secret, window_seconds=window_seconds, now=now)
    return subscription


def accepted_secrets(
    subscription: WebhookSubscription,
    *,
    now: datetime | None = None,
) -> list[str]:
    """Secrets a verifier should accept at ``now`` (newest first)."""
    if not isinstance(subscription, WebhookSubscription):
        raise ValueError("subscription must be a WebhookSubscription")
    return subscription.verification_secrets(now=now)


__all__ = [
    "DEFAULT_ROTATION_WINDOW_SECONDS",
    "MAX_ROTATION_WINDOW_SECONDS",
    "accepted_secrets",
    "rotate_secret",
]
