"""Tests for integration health snapshots and owner-scoped API (#684)."""

from __future__ import annotations

from app.models import Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_VIEWER, STATUS_ACTIVE
from app.services.integrations import health

WEBHOOK = "https://hooks.slack.com/services/T/B/secret"


class _Response:
    def __init__(self, status_code=200):
        self.status_code = status_code


def _workspace(db, owner):
    row = Workspace(user_id=owner.id, name="Health Workspace")
    db.session.add(row)
    db.session.commit()
    return row


def _configure_slack(monkeypatch, row, owner, url=WEBHOOK):
    from app.services.integrations import slack

    monkeypatch.setattr("app.services.job_queue.validate_webhook_url", lambda value: value)
    slack.configure(row.id, owner, url)


def test_record_success_and_error_are_non_secret(db, make_user):
    owner = make_user()
    row = _workspace(db, owner)

    health.record_error(
        row.id,
        "slack",
        f"failed to reach {WEBHOOK}",
        code="delivery_failed",
        status_code=503,
    )
    snapshot = health.get_health(row.id, "slack")
    assert snapshot["status"] == "error"
    assert snapshot["last_error_code"] == "delivery_failed"
    assert snapshot["last_status_code"] == 503
    assert "secret" not in (snapshot["last_error"] or "")
    assert "[redacted-url]" in snapshot["last_error"]

    health.record_success(row.id, "slack", status_code=200)
    snapshot = health.get_health(row.id, "slack")
    assert snapshot["status"] == "healthy"
    assert snapshot["last_success_at"] is not None
    assert snapshot["last_error_at"] is not None
    assert snapshot["last_status_code"] == 200


def test_list_integrations_reports_configuration_without_secrets(
    client, db, make_user, login, monkeypatch
):
    owner = make_user()
    login()
    row = _workspace(db, owner)
    _configure_slack(monkeypatch, row, owner)

    response = client.get(f"/integrations/api/workspaces/{row.id}/integrations")
    assert response.status_code == 200
    payload = response.get_json()
    slack_entry = next(item for item in payload["integrations"] if item["integration"] == "slack")
    assert slack_entry["configured"] is True
    assert "webhook_url" in slack_entry["credential_names"]
    assert WEBHOOK not in response.get_data(as_text=True)
    assert "encrypted_value" not in response.get_data(as_text=True)


def test_health_endpoint_returns_every_integration(client, db, make_user, login):
    owner = make_user()
    login()
    row = _workspace(db, owner)
    response = client.get(f"/integrations/api/workspaces/{row.id}/integrations/health")
    assert response.status_code == 200
    integrations = {item["integration"] for item in response.get_json()["health"]}
    assert integrations == {"slack", "teams", "email"}


def test_permissions_are_owner_only(client, db, make_user, login):
    owner = make_user()
    member = make_user(username="member", email="member@example.com")
    outsider = make_user(username="outsider", email="outsider@example.com")
    row = _workspace(db, owner)
    db.session.add(
        WorkspaceMember(
            workspace_id=row.id,
            user_id=member.id,
            role=ROLE_VIEWER,
            status=STATUS_ACTIVE,
        )
    )
    db.session.commit()
    base = f"/integrations/api/workspaces/{row.id}/integrations"

    login(email=member.email)
    assert client.get(base).status_code == 403
    assert client.put(f"{base}/slack", json={"webhook_url": WEBHOOK}).status_code == 403

    login(email=outsider.email)
    assert client.get(base).status_code == 404
    assert client.post(f"{base}/slack/test").status_code == 404


def test_configure_and_delete_integration(client, db, make_user, login, monkeypatch):
    owner = make_user()
    login()
    row = _workspace(db, owner)
    monkeypatch.setattr("app.services.job_queue.validate_webhook_url", lambda value: value)
    base = f"/integrations/api/workspaces/{row.id}/integrations/slack"

    invalid = client.put(base, json={})
    assert invalid.status_code == 400
    assert invalid.get_json()["code"] == "invalid_input"

    saved = client.put(base, json={"webhook_url": WEBHOOK})
    assert saved.status_code == 200
    assert saved.get_json()["configured"] is True

    deleted = client.delete(base)
    assert deleted.status_code == 200
    assert deleted.get_json()["configured"] is False


def test_manual_test_connection_records_success(client, db, make_user, login, monkeypatch):
    owner = make_user()
    login()
    row = _workspace(db, owner)
    _configure_slack(monkeypatch, row, owner)
    monkeypatch.setattr(
        "app.services.integrations.slack.requests.post",
        lambda *args, **kwargs: _Response(200),
    )

    response = client.post(f"/integrations/api/workspaces/{row.id}/integrations/slack/test")
    assert response.status_code == 200
    assert response.get_json()["status"] == "sent"
    assert health.get_health(row.id, "slack")["status"] == "healthy"


def test_manual_test_connection_reports_failure(client, db, make_user, login, monkeypatch):
    owner = make_user()
    login()
    row = _workspace(db, owner)
    _configure_slack(monkeypatch, row, owner)
    monkeypatch.setattr(
        "app.services.integrations.slack.requests.post",
        lambda *args, **kwargs: _Response(503),
    )
    monkeypatch.setattr("app.services.integrations.common.retry_delay_seconds", lambda attempt: 0)

    response = client.post(f"/integrations/api/workspaces/{row.id}/integrations/slack/test")
    assert response.status_code == 502
    payload = response.get_json()
    assert payload["status"] == "error"
    assert payload["code"] == "delivery_failed"
    assert WEBHOOK not in response.get_data(as_text=True)
