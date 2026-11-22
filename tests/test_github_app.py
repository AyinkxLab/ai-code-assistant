"""Tests for GitHub App installation lifecycle (#676)."""

from __future__ import annotations

import hashlib
import hmac
import json

import pytest

from app.extensions import db
from app.models import GitHubAppInstallation, Workspace
from app.services import github_app


@pytest.fixture()
def workspace(db, make_user):
    owner = make_user(username="owner", email="owner@example.com")
    row = Workspace(user_id=owner.id, name="GitHub App")
    db.session.add(row)
    db.session.commit()
    return row, owner


def test_installation_token_is_encrypted_and_redacted(workspace):
    ws, owner = workspace
    row = github_app.record_installation(
        ws.id,
        owner,
        12345,
        "octo",
        account_id=99,
        repository_selection="selected",
        repository_ids=[1, 2, 2],
        access_token="ghs_installation_secret",
    )

    assert row.status == "active"
    assert row.repository_ids() == [1, 2]
    assert "ghs_installation_secret" not in (row.access_token_encrypted or "")
    assert row.access_token() == "ghs_installation_secret"
    payload = json.dumps(row.to_dict())
    assert "ghs_installation_secret" not in payload
    assert row.access_token_encrypted not in payload
    assert row.to_dict()["has_access_token"] is True


def test_update_repository_selection_and_revoke_clears_token(workspace):
    ws, owner = workspace
    github_app.record_installation(
        ws.id,
        owner,
        1,
        "octo",
        repository_selection="selected",
        repository_ids=[10],
        access_token="token",
    )

    updated = github_app.update_installation(
        ws.id,
        owner,
        1,
        repository_selection="all",
        repository_ids=[10, 20],
    )
    assert updated.repository_selection == "all"
    assert updated.repository_ids() == []

    revoked = github_app.revoke_installation(ws.id, owner, 1)
    assert revoked.status == "revoked"
    assert revoked.revoked_at is not None
    assert revoked.access_token_encrypted is None


def test_non_owner_is_denied_and_installation_id_is_validated(workspace, make_user):
    ws, owner = workspace
    outsider = make_user(username="outsider", email="outsider@example.com")

    with pytest.raises(github_app.GitHubAppPermissionError) as exc:
        github_app.record_installation(ws.id, outsider, 1, "octo")
    assert exc.value.code == "permission_denied"

    with pytest.raises(github_app.GitHubAppValidationError):
        github_app.record_installation(ws.id, owner, 0, "octo")

    with pytest.raises(github_app.GitHubAppValidationError):
        github_app.record_installation(ws.id, owner, 1, "")


def test_installation_cannot_be_moved_to_another_workspace(workspace, make_user):
    ws, owner = workspace
    other = Workspace(user_id=owner.id, name="Other")
    db.session.add(other)
    db.session.commit()
    github_app.record_installation(ws.id, owner, 5, "octo")

    with pytest.raises(github_app.GitHubAppConflict):
        github_app.record_installation(other.id, owner, 5, "octo")


def test_owner_scoped_api_routes(client, db, make_user, login):
    owner = make_user()
    login()
    ws = Workspace(user_id=owner.id, name="Routes")
    db.session.add(ws)
    db.session.commit()
    base = f"/integrations/api/workspaces/{ws.id}/github-app/installations"

    created = client.post(
        base,
        json={
            "installation_id": 77,
            "account_login": "octo",
            "repository_selection": "selected",
            "repository_ids": [3, 4],
        },
    )
    assert created.status_code == 201, created.get_json()
    assert created.get_json()["installation_id"] == 77
    assert created.get_json()["repository_ids"] == [3, 4]

    listed = client.get(base)
    assert listed.status_code == 200
    assert len(listed.get_json()["installations"]) == 1

    patched = client.patch(
        f"{base}/77",
        json={"repository_selection": "all", "repository_ids": [3, 4]},
    )
    assert patched.status_code == 200
    assert patched.get_json()["repository_selection"] == "all"

    deleted = client.delete(f"{base}/77")
    assert deleted.status_code == 200
    assert deleted.get_json()["status"] == "revoked"

    other_user = make_user(username="other", email="other@example.com")
    other_ws = Workspace(user_id=other_user.id, name="Other")
    db.session.add(other_ws)
    db.session.commit()
    login(email="other@example.com")
    assert client.get(base).status_code == 403


def test_webhook_updates_and_revokes_installation(app, client, db, workspace):
    ws, owner = workspace
    github_app.record_installation(
        ws.id,
        owner,
        88,
        "octo",
        repository_selection="selected",
        repository_ids=[1],
        access_token="token",
    )
    app.config["GITHUB_APP_WEBHOOK_SECRET"] = "hook-secret"

    def deliver(event, payload):
        body = json.dumps(payload).encode()
        signature = "sha256=" + hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()
        return client.post(
            "/integrations/github-app/webhook",
            data=body,
            headers={
                "Content-Type": "application/json",
                "X-GitHub-Event": event,
                "X-Hub-Signature-256": signature,
            },
        )

    updated = deliver(
        "installation_repositories",
        {
            "action": "added",
            "installation": {"id": 88},
            "repositories": [{"id": 1}, {"id": 2}],
        },
    )
    assert updated.status_code == 202
    row = GitHubAppInstallation.query.filter_by(installation_id=88).one()
    assert row.repository_ids() == [1, 2]

    suspended = deliver(
        "installation",
        {"action": "suspend", "installation": {"id": 88}},
    )
    assert suspended.status_code == 202
    assert row.status == "suspended"
    assert row.access_token_encrypted is None

    deleted = deliver(
        "installation",
        {"action": "deleted", "installation": {"id": 88}},
    )
    assert deleted.status_code == 202
    assert row.status == "revoked"

    bad = client.post(
        "/integrations/github-app/webhook",
        data=b"{}",
        headers={"X-GitHub-Event": "installation", "X-Hub-Signature-256": "sha256=bad"},
    )
    assert bad.status_code == 401


def test_unknown_installation_webhook_is_acknowledged_without_creating_data(app, client, db):
    app.config["GITHUB_APP_WEBHOOK_SECRET"] = "hook-secret"
    payload = {"action": "deleted", "installation": {"id": 404}}
    body = json.dumps(payload).encode()
    signature = "sha256=" + hmac.new(b"hook-secret", body, hashlib.sha256).hexdigest()

    response = client.post(
        "/integrations/github-app/webhook",
        data=body,
        headers={
            "Content-Type": "application/json",
            "X-GitHub-Event": "installation",
            "X-Hub-Signature-256": signature,
        },
    )

    assert response.status_code == 202
    assert response.get_json()["handled"] is False
    assert GitHubAppInstallation.query.count() == 0
