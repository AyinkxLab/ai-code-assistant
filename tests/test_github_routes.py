"""Tests for GitHub integration routes: OAuth flow and JSON API.

The GitHub API is mocked at the ``requests`` layer so tests never hit the
network. The OAuth token-exchange POST is mocked the same way.
"""

import json
from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import GithubAccount, User
from app.services.crypto import decrypt_secret
from app.services.github import GitHubClient, GitHubNotFoundError


class FakeResponse:
    def __init__(self, status_code=200, data=None, text="", headers=None):
        self.status_code = status_code
        self._data = data
        self.text = text
        self.headers = headers or {}
        self.content = (text or json.dumps(data) if data is not None else "").encode()

    def json(self):
        return self._data


def _make_fake_session(script):
    ordered = sorted(script, key=lambda entry: len(entry[1]), reverse=True)

    class FakeSession:
        def __init__(self):
            self.headers = {"Authorization": "", "X-GitHub-Api-Version": "2022-11-28"}

        def request(self, method, url, params=None, timeout=None, **kwargs):
            url_path = url.split("api.github.com", 1)[-1].split("?", 1)[0]
            for entry in ordered:
                if entry[0] in (method, "*") and (entry[1] == "*" or entry[1] in url_path):
                    status, data = entry[2], entry[3]
                    headers = entry[4] if len(entry) > 4 else None
                    return FakeResponse(status, data, headers=headers)
            raise AssertionError(f"Unhandled request: {method} {url_path}")

        def get(self, url, params=None, timeout=None, **kwargs):
            return self.request("GET", url, params=params, timeout=timeout)

    return FakeSession()


def _logged_in_client(client):
    client.post(
        "/auth/register",
        data={
            "username": "ghuser",
            "email": "ghuser@example.com",
            "password": "supersecret123",
            "password_confirm": "supersecret123",
        },
        follow_redirects=True,
    )
    return client


def _create_account(app):
    user = User.query.filter_by(username="ghuser").first()
    account = GithubAccount(user_id=user.id, github_user_id=42, github_username="ghuser")
    account.set_access_token("ghn_test_token")
    db.session.add(account)
    db.session.commit()
    return account


def _last_session_state(client):
    with client.session_transaction() as session:
        return session.get("github_oauth_state")


class TestOAuthFlow:
    def test_connect_requires_login(self, client):
        response = client.get("/github/connect")
        assert response.status_code == 302

    def test_connect_redirects_to_github(self, client, app):
        app.config["GITHUB_CLIENT_ID"] = "client-id"
        _logged_in_client(client)
        response = client.get("/github/connect")
        assert response.status_code == 302
        assert response.headers["Location"].startswith("https://github.com/login/oauth/authorize")
        assert "client_id=client-id" in response.headers["Location"]

    def test_callback_without_code_redirects_with_flash(self, client):
        _logged_in_client(client)
        response = client.get("/github/callback", follow_redirects=True)
        assert b"missing code" in response.data

    def test_callback_state_mismatch_rejected(self, client):
        _logged_in_client(client)
        response = client.get(
            "/github/callback?code=abc&state=attacker-supplied", follow_redirects=True
        )
        assert b"state mismatch" in response.data

    def test_callback_stores_encrypted_token(self, client, app, monkeypatch):
        app.config["GITHUB_CLIENT_ID"] = "client-id"
        app.config["GITHUB_CLIENT_SECRET"] = "client-secret"
        _logged_in_client(client)

        client.get("/github/connect")  # sets the OAuth state in the session
        session_state = _last_session_state(client)

        monkeypatch.setattr(
            "app.github.routes.requests.post",
            lambda *a, **k: FakeResponse(
                200,
                {
                    "access_token": "gho_real_token",
                    "token_type": "bearer",
                    "scope": "read:user repo",
                },
            ),
        )
        monkeypatch.setattr(
            "app.services.github.requests.Session",
            lambda: _make_fake_session(),
        )

        response = client.get(
            f"/github/callback?code=abc&state={session_state}", follow_redirects=True
        )
        assert b"Connected to GitHub as" in response.data

        user = User.query.filter_by(username="ghuser").first()
        account = GithubAccount.query.filter_by(user_id=user.id).first()
        assert account is not None
        assert account.github_username == "ghuser"
        assert "gho_real_token" not in account.access_token_encrypted
        assert decrypt_secret(account.access_token_encrypted) == "gho_real_token"

    def test_callback_stores_refresh_token_and_expiry(self, client, app, monkeypatch):
        app.config["GITHUB_CLIENT_ID"] = "client-id"
        app.config["GITHUB_CLIENT_SECRET"] = "client-secret"
        _logged_in_client(client)
        client.get("/github/connect")
        session_state = _last_session_state(client)
        monkeypatch.setattr(
            "app.github.routes.requests.post",
            lambda *a, **k: FakeResponse(
                200,
                {
                    "access_token": "ghn_expiring",
                    "refresh_token": "ghr_refresh",
                    "expires_in": 1800,
                    "token_type": "bearer",
                },
            ),
        )
        monkeypatch.setattr(
            "app.services.github.requests.Session",
            lambda: _make_fake_session(),
        )

        response = client.get(f"/github/callback?code=abc&state={session_state}")
        assert response.status_code == 302
        account = GithubAccount.query.first()
        assert decrypt_secret(account.refresh_token_encrypted) == "ghn_refresh"
        assert account.token_expires_at.replace(tzinfo=UTC) > datetime.now(UTC)

    def test_expired_token_refreshes_and_persists_rotation(self, client, app, monkeypatch):
        app.config["GITHUB_CLIENT_ID"] = "client-id"
        app.config["GITHUB_CLIENT_SECRET"] = "client-secret"
        _logged_in_client(client)
        account = _create_account(app)
        account.set_refresh_token("ghn_old")
        account.token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.session.commit()

        calls = []

        def refresh(*args, **kwargs):
            calls.append(kwargs["data"])
            return FakeResponse(
                200,
                {"access_token": "gho_new", "refresh_token": "ghn_new", "expires_in": 3600},
            )

        monkeypatch.setattr("app.services.github.requests.post", refresh)
        monkeypatch.setattr(
            "app.services.github.requests.Session",
            lambda: _make_fake_session(
                [
                    ("GET", "/user/repos", 200, []),
                    ("GET", "/user", 200, {"id": 42, "login": "ghuser"}),
                ]
            ),
        )

        response = client.get("/github/api/repos")
        assert response.status_code == 200
        assert calls[0]["grant_type"] == "refresh_token"
        db.session.refresh(account)
        assert decrypt_secret(account.access_token_encrypted) == "gho_new"
        assert decrypt_secret(account.refresh_token_encrypted) == "ghn_new"

    def test_disconnect_attempts_revocation(self, client, app, monkeypatch):
        app.config["GITHUB_CLIENT_ID"] = "client-id"
        app.config["GITHUB_CLIENT_SECRET"] = "client-secret"
        _logged_in_client(client)
        _create_account(app)
        calls = []
        monkeypatch.setattr(
            "app.services.github.requests.delete",
            lambda *args, **kwargs: calls.append((args, kwargs)) or FakeResponse(204),
        )

        response = client.post("/github/disconnect", follow_redirects=True)
        assert response.status_code == 200
        assert calls[0][0][0].endswith("/applications/client-id/token")
        assert calls[0][1]["json"] == {"access_token": "ghn_test_token"}
        assert GithubAccount.query.count() == 0

    def test_disconnect_removes_account(self, client, app):
        _logged_in_client(client)
        _create_account(app)
        assert GithubAccount.query.count() == 1
        response = client.post("/github/disconnect", follow_redirects=True)
        assert b"Disconnected your GitHub account" in response.data
        assert GithubAccount.query.count() == 0

    def test_status_returns_connection(self, client, app, monkeypatch):
        _logged_in_client(client)
        _create_account(app)
        monkeypatch.setattr(
            "app.services.github.requests.Session",
            lambda: _make_fake_session(
                [
                    (
                        "GET",
                        "/rate_limit",
                        200,
                        {
                            "resources": {
                                "core": {
                                    "limit": 5000,
                                    "remaining": 4999,
                                    "reset": 1_700_000_000,
                                    "used": 1,
                                }
                            }
                        },
                    )
                ]
            ),
        )
        response = client.get("/github/api/status")
        data = response.get_json()
        assert data["connected"] is True
        assert data["account"]["github_username"] == "ghuser"
        assert "access_token" not in json.dumps(data)

    def test_status_includes_rate_limit_when_available(self, client, app, monkeypatch):
        _logged_in_client(client)
        _create_account(app)
        monkeypatch.setattr(
            "app.services.github.requests.Session",
            lambda: _make_fake_session(
                [
                    (
                        "GET",
                        "/rate_limit",
                        200,
                        {
                            "resources": {
                                "core": {
                                    "limit": 5000,
                                    "remaining": 4999,
                                    "reset": 1_700_000_000,
                                    "used": 1,
                                }
                            }
                        },
                    )
                ]
            ),
        )
        data = client.get("/github/api/status").get_json()
        rate_limit = data["rate_limit"]
        assert rate_limit["available"] is True
        assert rate_limit["remaining"] == 4999
        assert rate_limit["reset"] == 1_700_000_000
        assert rate_limit["low"] is False
        serialized = json.dumps(data)
        assert "gho_test_token" not in serialized
        assert "access_token" not in serialized

    def test_status_flags_low_quota(self, client, app, monkeypatch):
        _logged_in_client(client)
        _create_account(app)
        monkeypatch.setattr(
            "app.services.github.requests.Session",
            lambda: _make_fake_session(
                [
                    (
                        "GET",
                        "/rate_limit",
                        200,
                        {
                            "resources": {
                                "core": {
                                    "limit": 5000,
                                    "remaining": 5,
                                    "reset": 1_700_000_000,
                                    "used": 4995,
                                }
                            }
                        },
                    )
                ]
            ),
        )
        data = client.get("/github/api/status").get_json()
        assert data["rate_limit"]["low"] is True


class TestCompareRoute:
    def test_compare_requires_login(self/ client):
        response = client.get("/github/api/compare/ow/repo/main...feature")
        assert response.status_code == 302

    def test_compare_returns_file_diff(self/ client, app, monkeypatch):
        _logged_in_client(client)
        _create_account(app)
        compare_payload = {
            "status": "ahead",
            "ahead_by": 2,
            "behind_by": 0,
            "total_commits": 2,
            "commits": [
                {"sha": "abc123", "commit": {"message": "Add feature"}},
                {"sha": "def456", "commit": {"message": "Fix bug"}},
            ],
            "files": [
                {
                    "filename": "app/py.py",
                    "status": "modified",
                    "additions": 10,
                    "deletions": 2,
                    "changes": 12,
                    "patch": "@@ -1,+1 @@\n+old\n-new",
                },
                {
                    "filename": "README.md",
                    "status": "added",
                    "additions": 5,
                    "deletions": 0,
                    "changes": 5,
                    "patch": "@@ -0,0 +1,5 @@\n+hello",
                },
            ],
        }
        monkeypatch.setattr(
            "app.services.github.requests.Session",
            lambda: _make_fake_session(
                [
                    ("GET", "/repos/ow/repo/compare/main...feature", 200, compare_payload),
                ]
            ),
        )
        response = client.get("/github/api/compare/ow/repo/main...feature")
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "ahead"
        assert data["ahead_by"] == 2
        assert len(data["files"]) == 2
        filenames = {f ["filename"] for f in data["files"]}
        assert filenames == {"app/py.py", "README.md"}
        assert data["additions"] == 15
        assert data["deletions"] == 2

