"""Tests for webhook signing-secret rotation (#673)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import WebhookSubscription, Workspace
from app.models.webhook import hash_webhook_secret
from app.services import webhook_secrets


def _subscription(db, make_user):
    user = make_user()
    workspace = Workspace(user_id=user.id, name="Demo", slug="demo")
    db.session.add(workspace)
    db.session.flush()
    subscription = WebhookSubscription(
        workspace_id=workspace.id,
        url="https://example.com/hook",
        event_type="*",
        is_active=True,
    )
    subscription.set_secret("old-secret")
    db.session.add(subscription)
    db.session.commit()
    return subscription


def test_rotation_keeps_the_old_secret_for_the_window(db, make_user):
    subscription = _subscription(db, make_user)
    now = datetime(2026, 1, 1, tzinfo=UTC)

    webhook_secrets.rotate_secret(subscription, "new-secret", window_seconds=60, now=now)
    db.session.commit()

    assert subscription.signing_secret() == "new-secret"
    assert subscription.secret_hash == hash_webhook_secret("new-secret")
    assert webhook_secrets.accepted_secrets(subscription, now=now) == [
        "new-secret",
        "old-secret",
    ]

    expired = now + timedelta(seconds=61)
    assert webhook_secrets.accepted_secrets(subscription, now=expired) == ["new-secret"]


def test_rotation_validates_input(db, make_user):
    subscription = _subscription(db, make_user)
    with pytest.raises(ValueError):
        webhook_secrets.rotate_secret(subscription, "")
    with pytest.raises(ValueError):
        webhook_secrets.rotate_secret(subscription, "x", window_seconds=90_000)
    with pytest.raises(ValueError):
        webhook_secrets.rotate_secret("not-a-subscription", "x")
