"""Tests for webhook subscription CRUD and the dead-letter queue (#668, #671)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.models import WebhookDeadLetter, WebhookSubscription, Workspace
from app.services import webhook_dead_letters


def _workspace(db, user, name="Hooks"):
    workspace = Workspace(user_id=user.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _subscription(db, workspace, url="https://example.com/hook"):
    subscription = WebhookSubscription(
        workspace_id=workspace.id, url=url, event_type="review.completed", secret="s3cret"
    )
    db.session.add(subscription)
    db.session.commit()
    return subscription


def test_create_returns_secret_once_and_list_hides_it(client, db, make_user, login, monkeypatch):
    owner = make_user()
    login()
    workspace = _workspace(db, owner)
    monkeypatch.setattr("app.services.job_queue.validate_webhook_url", lambda value: value.strip())
    base = f"/webhooks/api/workspaces/{workspace.id}/subscriptions"

    created = client.post(
        base,
        json={"url": "https://example.com/hook", "event_type": "review.completed"},
    )
    assert created.status_code == 201
    body = created.get_json()
    assert body["secret"]
    assert body["url"] == "https://example.com/hook"

    listed = client.get(base)
    assert listed.status_code == 200
    items = listed.get_json()["subscriptions"]
    assert len(items) == 1
    assert "secret" not in items[0]
    assert items[0]["event_type"] == "review.completed"


def test_update_and_delete_subscription(client, db, make_user, login, monkeypatch):
    owner = make_user()
    login()
    workspace = _workspace(db, owner)
    subscription = _subscription(db, workspace)
    monkeypatch.setattr("app.services.job_queue.validate_webhook_url", lambda value: value.strip())
    base = f"/webhooks/api/workspaces/{workspace.id}/subscriptions/{subscription.id}"

    updated = client.patch(base, json={"event_type": "project.created", "is_active": False})
    assert updated.status_code == 200
    assert updated.get_json()["event_type"] == "project.created"
    assert updated.get_json()["is_active"] is False

    deleted = client.delete(base)
    assert deleted.status_code == 200
    assert deleted.get_json() == {"deleted": True}
    assert db.session.get(WebhookSubscription, subscription.id) is None


def test_invalid_url_and_owner_scoping(client, db, make_user, login, monkeypatch):
    owner = make_user()
    make_user(username="other", email="other@example.com")
    login()
    workspace = _workspace(db, owner)
    base = f"/webhooks/api/workspaces/{workspace.id}/subscriptions"

    def reject(_value):
        from app.services.job_queue import JobQueueError

        raise JobQueueError("webhook host must be publicly routable")

    monkeypatch.setattr("app.services.job_queue.validate_webhook_url", reject)
    invalid = client.post(base, json={"url": "http://127.0.0.1/hook"})
    assert invalid.status_code == 400
    assert invalid.get_json()["code"] == "invalid_webhook_config"

    login(email="other@example.com")
    assert client.get(base).status_code == 404


def test_dead_letters_can_be_listed_and_replayed(client, db, make_user, login, monkeypatch):
    owner = make_user()
    login()
    workspace = _workspace(db, owner)
    subscription = _subscription(db, workspace)
    now = datetime.now(UTC)
    first = webhook_dead_letters.record_failure(
        subscription,
        event_type="review.completed",
        payload={"review_id": 1},
        response_code=500,
        error="HTTP 500",
        now=now,
    )
    second = webhook_dead_letters.record_failure(
        subscription,
        event_type="review.completed",
        payload={"review_id": 2},
        now=now,
    )
    base = (
        f"/webhooks/api/workspaces/{workspace.id}/subscriptions/" f"{subscription.id}/dead-letters"
    )

    listed = client.get(base)
    assert listed.status_code == 200
    ids = [row["id"] for row in listed.get_json()["dead_letters"]]
    assert ids == [second.id, first.id]
    assert all("payload" not in row for row in listed.get_json()["dead_letters"])

    enqueued = []
    monkeypatch.setattr(
        "app.services.job_queue.enqueue", lambda kind, payload: enqueued.append(payload)
    )
    replay = client.post(f"{base}/replay", json={"ids": [first.id, second.id]})
    assert replay.status_code == 200
    result = replay.get_json()
    assert result["replayed"] == 2
    assert result["duplicates"] == 0
    assert len(enqueued) == 2

    again = client.post(f"{base}/replay", json={"ids": [first.id]})
    assert again.status_code == 200
    assert again.get_json()["duplicates"] == 1


def test_expired_dead_letters_are_not_replayed(client, db, make_user, login, monkeypatch):
    owner = make_user()
    login()
    workspace = _workspace(db, owner)
    subscription = _subscription(db, workspace)
    now = datetime.now(UTC)
    expired = WebhookDeadLetter(
        subscription_id=subscription.id,
        event_type="review.completed",
        payload='{"review_id": 1}',
        attempts=3,
        expires_at=now - timedelta(seconds=1),
        created_at=now - timedelta(days=8),
    )
    db.session.add(expired)
    db.session.commit()
    monkeypatch.setattr("app.services.job_queue.enqueue", lambda kind, payload: None)
    base = (
        f"/webhooks/api/workspaces/{workspace.id}/subscriptions/" f"{subscription.id}/dead-letters"
    )
    response = client.post(f"{base}/replay", json={"ids": [expired.id]})
    assert response.status_code == 200
    assert response.get_json()["expired"] == 1
    assert response.get_json()["replayed"] == 0


def test_replay_rejects_invalid_batches(client, db, make_user, login):
    owner = make_user()
    login()
    workspace = _workspace(db, owner)
    subscription = _subscription(db, workspace)
    base = (
        f"/webhooks/api/workspaces/{workspace.id}/subscriptions/" f"{subscription.id}/dead-letters"
    )
    for body in ({"ids": []}, {"ids": [0]}, {"ids": "nope"}):
        response = client.post(f"{base}/replay", json=body)
        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_replay_request"
