"""Tests for the webhook subscription model extensions (#667)."""

from __future__ import annotations

import pytest

from app.models import WebhookSubscription, Workspace
from app.models.webhook import (
    hash_webhook_secret,
    normalize_event_types,
)


def _subscription(workspace, url="https://example.com/hook"):
    subscription = WebhookSubscription(
        workspace_id=workspace.id,
        url=url,
        event_type="*",
        is_active=True,
    )
    subscription.set_secret("whsec_test_secret")
    subscription.set_events(["analysis.completed", "review.completed"])
    return subscription


def test_normalizes_and_validates_event_filters():
    assert normalize_event_types("Analysis.Completed") == ["analysis.completed"]
    assert normalize_event_types(
        ["review.completed", "analysis.completed", "review.completed"]
    ) == ["analysis.completed", "review.completed"]
    with pytest.raises(ValueError):
        normalize_event_types([])
    with pytest.raises(ValueError):
        normalize_event_types([""])
    with pytest.raises(ValueError):
        normalize_event_types(["bad event"])


def test_hashes_secret_without_serializing_it():
    digest = hash_webhook_secret("whsec_test_secret")
    assert len(digest) == 64
    assert digest == hash_webhook_secret("whsec_test_secret")
    with pytest.raises(ValueError):
        hash_webhook_secret("")


def test_subscription_round_trip_and_serialization(db, make_user):
    user = make_user()
    workspace = Workspace(user_id=user.id, name="Demo", slug="demo")
    db.session.add(workspace)
    db.session.flush()
    subscription = _subscription(workspace)
    db.session.add(subscription)
    db.session.commit()

    stored = db.session.get(WebhookSubscription, subscription.id)
    assert stored is not None
    assert stored.event_types() == ["analysis.completed", "review.completed"]
    assert stored.matches("analysis.completed")
    assert not stored.matches("deployment.verified")
    assert stored.signing_secret() == "whsec_test_secret"
    assert stored.secret_hash == hash_webhook_secret("whsec_test_secret")

    data = stored.to_dict()
    assert data["events"] == ["analysis.completed", "review.completed"]
    assert data["has_secret"] is True
    assert "secret" not in data
    assert "secret_hash" not in data

    stored.is_active = False
    assert not stored.matches("analysis.completed")


def test_legacy_single_event_type_still_matches(db, make_user):
    user = make_user()
    workspace = Workspace(user_id=user.id, name="Demo", slug="demo")
    db.session.add(workspace)
    db.session.flush()
    subscription = WebhookSubscription(
        workspace_id=workspace.id,
        url="https://example.com/hook",
        event_type="analysis.completed",
        is_active=True,
    )
    db.session.add(subscription)
    db.session.commit()

    assert subscription.event_types() == ["analysis.completed"]
    assert subscription.matches("analysis.completed")
    assert not subscription.matches("review.completed")


def test_events_are_deduplicated_in_the_database(db, make_user):
    user = make_user()
    workspace = Workspace(user_id=user.id, name="Demo", slug="demo")
    db.session.add(workspace)
    db.session.flush()
    subscription = _subscription(workspace)
    db.session.add(subscription)
    db.session.commit()

    assert len(subscription.event_filters) == 2
    with pytest.raises(ValueError):
        subscription.set_events(["bad event"])
