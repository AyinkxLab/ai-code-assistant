"""Tests for GitLab OAuth connection and repository listing (#677)."""

from __future__ import annotations

from urllib.parse import parse_qs, urlparse

from app.models import GitlabAccount, IntegrationCredential, Workspace
from app.services import integration_credentials


def _workspace(db, user, name="GitLab"):
    workspace = Workspace(user_id=user.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _configure(app):
    app.config.update(
        GITLAB_CLIENT_ID="test-client",
        GITLAB_CLIENT_SECRET="test-secret",
        GITLAB_REDIRECT_URI="http://localhost/gitlab/callback",
        GITLAB_OAUTH_AUTHORIZE_URL="https://gitlab.example/oauth/authorize",
        GITLAB_OAUTH_TOKEN_URL="https://gitlab.example/oauth/token",
        GITLAB_OAUTH_REVOKE_URL="https://gitlab.example/oauth/revoke",
        GITLAB_API_URL="https://gitlab.example/api/v4",
        GITLAB_SCOPES="read_api",
    )


def _start_connect(client, workspace):
    response = client.get(f"/gitlab/workspaces/{workspace.id}/connect")
    assert response.status_code == 302
    location = response.headers["Location"]
    state = parse_qs(urlparse(location).query)["state"][0]
    return location, state


def test_connect_redirects_with_state_and_callback_stores_encrypted_token(
    app, client, db, make_user, login, monkeypatch
):
    _configure(app)
    owner = make_user()
    login()
    workspace = _workspace(db, owner)

    location, state = _start_connect(client, workspace)
    assert "client_id=test-client" in location
    assert "scope=read_api" in location

    monkeypatch.setattr("app.services.gitlab.exchange_code", lambda code: "tok-secret")
    monkeypatch.setattr(
        "app.services.gitlab.fetch_user",
        lambda token: {"id": 7, "username": "alice", "name": "Alice"},
    )
    callback = client.get(f"/gitlab/callback?state={state}&code=abc")
    assert callback.status_code == 200
    body = callback.get_json()
    assert body["username"] == "alice"
    assert "tok-secret" not in callback.get_data(as_text=True)

    credential = IntegrationCredential.query.filter_by(
        workspace_id=workspace.id, integration="gitlab", name="access_token"
    ).one()
    assert credential.encrypted_value != "tok-secret"
    assert integration_credentials.decrypt_for_use(credential) == "tok-secret"
    assert GitlabAccount.query.filter_by(workspace_id=workspace.id, gitlab_user_id=7).count() == 1


def test_callback_rejects_invalid_state(client, db, make_user, login, app):
    _configure(app)
    owner = make_user()
    login()
    _workspace(db, owner)
    response = client.get("/gitlab/callback?state=wrong&code=abc")
    assert response.status_code == 400
    assert response.get_json()["code"] == "invalid_oauth_state"


def test_repositories_and_revocation(app, client, db, make_user, login, monkeypatch):
    _configure(app)
    owner = make_user()
    login()
    workspace = _workspace(db, owner)
    monkeypatch.setattr("app.services.gitlab.exchange_code", lambda code: "tok-secret")
    monkeypatch.setattr(
        "app.services.gitlab.fetch_user",
        lambda token: {"id": 7, "username": "alice", "name": "Alice"},
    )
    _, state = _start_connect(client, workspace)
    assert client.get(f"/gitlab/callback?state={state}&code=abc").status_code == 200

    monkeypatch.setattr(
        "app.services.gitlab.list_repositories",
        lambda token: [{"id": 1, "name": "Repo", "path_with_namespace": "alice/repo"}],
    )
    repositories = client.get(f"/gitlab/api/workspaces/{workspace.id}/repositories")
    assert repositories.status_code == 200
    assert repositories.get_json()["repositories"][0]["name"] == "Repo"

    monkeypatch.setattr("app.services.gitlab.revoke_token", lambda token: True)
    revoked = client.post(f"/gitlab/api/workspaces/{workspace.id}/revoke")
    assert revoked.status_code == 200
    assert revoked.get_json() == {"revoked": True}
    assert (
        IntegrationCredential.query.filter_by(
            workspace_id=workspace.id, integration="gitlab"
        ).count()
        == 0
    )
    account = GitlabAccount.query.filter_by(workspace_id=workspace.id).one()
    assert account.revoked_at is not None


def test_owner_scoping(app, client, db, make_user, login):
    _configure(app)
    make_user()
    make_user(username="other", email="other@example.com")
    login()
    owner = make_user(username="owner", email="owner@example.com")
    workspace = _workspace(db, owner)
    login(email="other@example.com")
    assert client.get(f"/gitlab/workspaces/{workspace.id}/connect").status_code == 404
