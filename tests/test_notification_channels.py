"""Tests for workspace notification channel preferences (#631)."""

from __future__ import annotations

from app.models import Notification, Workspace, WorkspaceMember
from app.models.notification_channel import EVENT_TYPES
from app.models.workspace_member import ROLE_VIEWER, STATUS_ACTIVE


def _workspace(db, user, name="Notifications"):
    workspace = Workspace(user_id=user.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def test_matrix_defaults_and_update(client, db, make_user, login):
    owner = make_user()
    login()
    workspace = _workspace(db, owner)
    base = f"/workspaces/api/workspaces/{workspace.id}/notification-channels"

    initial = client.get(base)
    assert initial.status_code == 200
    payload = initial.get_json()
    assert len(payload["rows"]) == len(EVENT_TYPES)
    assert all(row["chat"] for row in payload["rows"])

    updated = client.put(
        base,
        json={
            "rows": [
                {
                    "event_type": "budget_alert",
                    "email": True,
                    "chat": False,
                    "webhook": True,
                }
            ]
        },
    )
    assert updated.status_code == 200
    row = next(item for item in updated.get_json()["rows"] if item["event_type"] == "budget_alert")
    assert row == {
        "event_type": "budget_alert",
        "email": True,
        "chat": False,
        "webhook": True,
    }


def test_invalid_configuration_returns_structured_errors(client, db, make_user, login):
    owner = make_user()
    login()
    workspace = _workspace(db, owner)
    base = f"/workspaces/api/workspaces/{workspace.id}/notification-channels"

    for body in (
        {"rows": [{"event_type": "nope", "email": True, "chat": True, "webhook": True}]},
        {"rows": [{"event_type": "mention", "email": "yes", "chat": True, "webhook": True}]},
        {"rows": "not-a-list"},
        {"rows": []},
    ):
        response = client.put(base, json=body)
        assert response.status_code == 400
        payload = response.get_json()
        assert payload["code"] == "invalid_channel_config"
        assert payload["error"]


def test_permissions_are_owner_scoped(client, db, make_user, login):
    owner = make_user()
    member_user = make_user(username="member", email="member@example.com")
    outsider = make_user(username="outsider", email="outsider@example.com")
    workspace = _workspace(db, owner)
    db.session.add(
        WorkspaceMember(
            workspace_id=workspace.id,
            user_id=member_user.id,
            role=ROLE_VIEWER,
            status=STATUS_ACTIVE,
        )
    )
    db.session.commit()
    base = f"/workspaces/api/workspaces/{workspace.id}/notification-channels"

    login(email=member_user.email)
    assert client.get(base).status_code == 403

    login(email=outsider.email)
    assert client.get(base).status_code == 404


def test_page_renders_the_preference_matrix(client, db, make_user, login):
    owner = make_user()
    login()
    workspace = _workspace(db, owner)
    response = client.get(f"/workspaces/{workspace.id}/notifications")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Notification channels" in body
    assert "budget_alert" in body


def test_chat_test_send_creates_a_notification(client, db, make_user, login):
    owner = make_user()
    login()
    workspace = _workspace(db, owner)
    before = Notification.query.filter_by(user_id=owner.id).count()
    response = client.post(
        f"/workspaces/api/workspaces/{workspace.id}/notification-channels/test",
        json={"event_type": "budget_alert", "channel": "chat"},
    )
    assert response.status_code == 200
    assert response.get_json()["status"] == "queued"
    assert Notification.query.filter_by(user_id=owner.id).count() == before + 1


def test_email_test_send_reports_when_unconfigured(client, db, make_user, login, monkeypatch):
    owner = make_user()
    login()
    workspace = _workspace(db, owner)
    monkeypatch.setattr("app.services.email._smtp_configured", lambda: False)
    response = client.post(
        f"/workspaces/api/workspaces/{workspace.id}/notification-channels/test",
        json={"event_type": "budget_alert", "channel": "email"},
    )
    assert response.status_code == 200
    payload = response.get_json()
    assert payload["status"] == "skipped"
    assert "not configured" in payload["reason"]


def test_webhook_test_send_validates_and_enqueues(client, db, make_user, login, monkeypatch):
    owner = make_user()
    login()
    workspace = _workspace(db, owner)
    url = f"/workspaces/api/workspaces/{workspace.id}/notification-channels/test"

    invalid = client.post(url, json={"event_type": "budget_alert", "channel": "webhook"})
    assert invalid.status_code == 200
    assert invalid.get_json()["status"] == "skipped"

    class FakeJob:
        id = 7

    monkeypatch.setattr("app.services.job_queue.validate_webhook_url", lambda value: value)
    monkeypatch.setattr("app.services.job_queue.enqueue", lambda kind, payload: FakeJob())
    queued = client.post(
        url,
        json={
            "event_type": "budget_alert",
            "channel": "webhook",
            "webhook_url": "https://example.com/hook",
        },
    )
    assert queued.status_code == 200
    payload = queued.get_json()
    assert payload["status"] == "queued"
    assert payload["job_id"] == 7
