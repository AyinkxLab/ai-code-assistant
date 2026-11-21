"""Tests for webhook delivery logs and their UI/API (#670, #672)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.models import WebhookDelivery, WebhookSubscription, Workspace
from app.services import webhook_deliveries

DELIVERY_KEYS = {
    "id",
    "subscription_id",
    "event_type",
    "attempt",
    "status",
    "response_code",
    "latency_ms",
    "error",
    "created_at",
}


def _subscription(db, user, *, secret="super-secret-token"):
    workspace = Workspace(user_id=user.id, name="Webhook workspace")
    db.session.add(workspace)
    db.session.commit()
    subscription = WebhookSubscription(
        workspace_id=workspace.id,
        url="https://example.com/hooks/events",
        event_type="review.completed",
        secret=secret,
    )
    db.session.add(subscription)
    db.session.commit()
    return subscription


def _deliveries(db, subscription, now):
    for index in range(5):
        db.session.add(
            WebhookDelivery(
                subscription_id=subscription.id,
                event_type="review.completed",
                attempt=1,
                status="success" if index % 2 == 0 else "failure",
                response_code=200 if index % 2 == 0 else 500,
                latency_ms=10 + index,
                error=None if index % 2 == 0 else "HTTP 500",
                created_at=now - timedelta(days=index),
            )
        )
    db.session.commit()


def test_api_filters_by_status_and_paginates(client, db, make_user, login):
    user = make_user()
    login()
    subscription = _subscription(db, user)
    _deliveries(db, subscription, datetime.now(UTC))

    first = client.get(
        f"/webhooks/api/subscriptions/{subscription.id}/deliveries"
        "?status=failure&page=1&per_page=1"
    )
    assert first.status_code == 200
    payload = first.get_json()
    assert payload["total"] == 2
    assert payload["page"] == 1
    assert payload["has_next"] is True
    assert payload["has_prev"] is False
    assert len(payload["items"]) == 1
    assert payload["items"][0]["status"] == "failure"

    second = client.get(
        f"/webhooks/api/subscriptions/{subscription.id}/deliveries"
        "?status=failure&page=2&per_page=1"
    )
    assert second.status_code == 200
    second_payload = second.get_json()
    assert second_payload["has_next"] is False
    assert second_payload["has_prev"] is True


def test_api_filters_by_date_range(client, db, make_user, login):
    user = make_user()
    login()
    subscription = _subscription(db, user)
    now = datetime.now(UTC)
    _deliveries(db, subscription, now)

    start = (now - timedelta(days=2)).date().isoformat()
    end = now.date().isoformat()
    response = client.get(
        f"/webhooks/api/subscriptions/{subscription.id}/deliveries" f"?start={start}&end={end}"
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["total"] == 3


def test_api_returns_structured_errors_for_bad_filters(client, db, make_user, login):
    user = make_user()
    login()
    subscription = _subscription(db, user)

    for query in ("status=bogus", "start=not-a-date", "page=abc"):
        response = client.get(f"/webhooks/api/subscriptions/{subscription.id}/deliveries?{query}")
        assert response.status_code == 400
        body = response.get_json()
        assert body["code"] == "invalid_filter"
        assert body["error"]


def test_delivery_log_never_exposes_secrets_or_bodies(client, db, make_user, login):
    user = make_user()
    login()
    subscription = _subscription(db, user)
    _deliveries(db, subscription, datetime.now(UTC))

    response = client.get(f"/webhooks/api/subscriptions/{subscription.id}/deliveries")
    assert response.status_code == 200
    raw = response.get_data(as_text=True)
    assert "super-secret-token" not in raw
    assert "password" not in raw.lower()
    for item in response.get_json()["items"]:
        assert set(item) == DELIVERY_KEYS


def test_page_renders_and_is_owner_scoped(client, db, make_user, login):
    owner = make_user()
    make_user(username="other", email="other@example.com")
    login()
    subscription = _subscription(db, owner)
    _deliveries(db, subscription, datetime.now(UTC))

    page = client.get(f"/webhooks/subscriptions/{subscription.id}/deliveries")
    assert page.status_code == 200
    body = page.get_data(as_text=True)
    assert "Delivery attempts" in body
    assert "super-secret-token" not in body

    login(email="other@example.com")
    denied = client.get(f"/webhooks/subscriptions/{subscription.id}/deliveries")
    assert denied.status_code == 404


def test_index_lists_only_the_current_users_subscriptions(client, db, make_user, login):
    owner = make_user()
    make_user(username="other", email="other@example.com")
    mine = _subscription(db, owner)
    subscription_id = mine.id
    login()

    response = client.get("/webhooks/")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert f"#{subscription_id}" in body
    assert "review.completed" in body

    login(email="other@example.com")
    other_response = client.get("/webhooks/")
    assert other_response.status_code == 200
    assert f"#{subscription_id}" not in other_response.get_data(as_text=True)


def test_record_delivery_validates_and_truncates(db, make_user):
    user = make_user()
    subscription = _subscription(db, user)

    with pytest.raises(ValueError):
        webhook_deliveries.record_delivery(subscription, event_type="x", status="weird")
    with pytest.raises(ValueError):
        webhook_deliveries.record_delivery(
            subscription, event_type="x", status="failure", attempt=0
        )

    row = webhook_deliveries.record_delivery(
        subscription,
        event_type="review.completed",
        status="failure",
        attempt=2,
        response_code=503,
        latency_ms=12.6,
        error="x" * 900,
    )
    assert row.attempt == 2
    assert row.response_code == 503
    assert row.latency_ms == 12
    assert len(row.error) == webhook_deliveries.MAX_ERROR_CHARS
