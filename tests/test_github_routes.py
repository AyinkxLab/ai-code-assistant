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
                    return FakeResponse(status, data)
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
    account.set_access_token("gho_test_token")
    db.session.add(account)
    db.session.commit()
    return account


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
            lambda: _make_fake_session([("GET", "/user", 200, {"id": 42, "login": "ghuser"})]),
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
                    "access_token": "gho_expiring",
                    "refresh_token": "ghr_refresh",
                    "expires_in": 1800,
                    "token_type": "bearer",
                },
            ),
        )
        monkeypatch.setattr(
            "app.services.github.requests.Session",
            lambda: _make_fake_session([("GET", "/user", 200, {"id": 42, "login": "ghuser"})]),
        )

        response = client.get(f"/github/callback?code=abc&state={session_state}")
        assert response.status_code == 302
        account = GithubAccount.query.first()
        assert decrypt_secret(account.refresh_token_encrypted) == "ghr_refresh"
        assert account.token_expires_at.replace(tzinfo=UTC) > datetime.now(UTC)

    def test_expired_token_refreshes_and_persists_rotation(self, client, app, monkeypatch):
        app.config["GITHUB_CLIENT_ID"] = "client-id"
        app.config["GITHUB_CLIENT_SECRET"] = "client-secret"
        _logged_in_client(client)
        account = _create_account(app)
        account.set_refresh_token("ghr_old")
        account.token_expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.session.commit()

        calls = []

        def refresh(*args, **kwargs):
            calls.append(kwargs["data"])
            return FakeResponse(
                200,
                {"access_token": "gho_new", "refresh_token": "ghr_new", "expires_in": 3600},
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
        assert decrypt_secret(account.refresh_token_encrypted) == "ghr_new"

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
        assert calls[0][1]["json"] == {"access_token": "gho_test_token"}
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
        rate_limit = client.get("/github/api/status").get_json()["rate_limit"]
        assert rate_limit["available"] is True
        assert rate_limit["remaining"] == 5
        assert rate_limit["low"] is True

    def test_status_reports_unavailable_quota(self, client, app, monkeypatch):
        _logged_in_client(client)
        _create_account(app)
        monkeypatch.setattr(
            "app.services.github.requests.Session",
            lambda: _make_fake_session([("GET", "/rate_limit", 200, {"resources": {}})]),
        )
        rate_limit = client.get("/github/api/status").get_json()["rate_limit"]
        assert rate_limit == {"available": False}

    def test_status_when_disconnected(self, client):
        _logged_in_client(client)
        data = client.get("/github/api/status").get_json()
        assert data["connected"] is False
        assert "rate_limit" not in data


class TestRepositoryApi:
    def test_repos_requires_connection(self, client):
        _logged_in_client(client)
        response = client.get("/github/api/repos")
        assert response.status_code == 403
        assert response.get_json()["kind"] == "not_connected"

    def test_repos_lists_and_filters(self, client, app, monkeypatch):
        _logged_in_client(client)
        _create_account(app)

        def fake_repos(self_, *, per_page=100):
            return [
                {
                    "full_name": "owner/api-repo",
                    "name": "api-repo",
                    "pushed_at": "2026-01-01T00:00:00Z",
                },
                {"full_name": "owner/other", "name": "other", "pushed_at": "2025-01-01T00:00:00Z"},
            ]

        monkeypatch.setattr(GitHubClient, "list_repositories", fake_repos)
        data = client.get("/github/api/repos").get_json()
        assert [r["full_name"] for r in data] == ["owner/api-repo", "owner/other"]
        assert data[0]["name"] == "api-repo"

        data = client.get("/github/api/repos?q=other").get_json()
        assert [r["full_name"] for r in data] == ["owner/other"]

    def test_repos_filters_by_name_and_description(self, client, app, monkeypatch):
        _logged_in_client(client)
        _create_account(app)

        def fake_repos(self_, *, per_page=100):
            return [
                {
                    "full_name": "owner/api-repo",
                    "name": "api-repo",
                    "description": "A REST service for billing",
                    "pushed_at": "2026-01-02T00:00:00Z",
                },
                {
                    "full_name": "owner/other",
                    "name": "other",
                    "description": "Experimental widgets",
                    "pushed_at": "2026-01-01T00:00:00Z",
                },
            ]

        monkeypatch.setattr(GitHubClient, "list_repositories", fake_repos)

        # Empty query returns every one of the user's repositories.
        data = client.get("/github/api/repos").get_json()
        assert [r["full_name"] for r in data] == ["owner/api-repo", "owner/other"]

        # Name match.
        data = client.get("/github/api/repos?q=widgets").get_json()
        assert [r["full_name"] for r in data] == ["owner/other"]

        # Description match (case-insensitive).
        data = client.get("/github/api/repos?q=BILLING").get_json()
        assert [r["full_name"] for r in data] == ["owner/api-repo"]

        # No match.
        assert client.get("/github/api/repos?q=nope").get_json() == []

    def test_repo_detail_not_found(self, client, app, monkeypatch):
        _logged_in_client(client)
        _create_account(app)

        def raise_not_found(self_, full_name):
            raise GitHubNotFoundError("The requested GitHub resource was not found.")

        monkeypatch.setattr(GitHubClient, "get_repository", raise_not_found)
        response = client.get("/github/api/repos/owner/missing")
        assert response.status_code == 404
        assert response.get_json()["kind"] == "not_found"


class TestApiEndpoints:
    def _script_client(self, client, app, script, monkeypatch):
        _logged_in_client(client)
        _create_account(app)
        monkeypatch.setattr(
            "app.services.github.requests.Session",
            lambda: _make_fake_session(script),
        )
        return client

    def test_tree_returns_entries(self, client, app, monkeypatch):
        script = [
            (
                "GET",
                "/git/trees/",
                200,
                {"tree": [{"path": "app/__init__.py", "type": "blob"}], "truncated": False},
            )
        ]
        self._script_client(client, app, script, monkeypatch)
        data = client.get("/github/api/repos/owner/repo/tree?ref=main").get_json()
        assert data["entries"][0]["path"] == "app/__init__.py"

    def test_commits_returns_summary(self, client, app, monkeypatch):
        script = [
            (
                "GET",
                "/commits",
                200,
                [
                    {
                        "sha": "abc123",
                        "commit": {
                            "message": "Fix the bug\n",
                            "author": {"name": "Alice", "date": "2026-01-01T00:00:00Z"},
                        },
                    }
                ],
            )
        ]
        self._script_client(client, app, script, monkeypatch)
        data = client.get("/github/api/repos/owner/repo/commits").get_json()
        assert data[0]["short_sha"] == "abc123"
        assert data[0]["message"] == "Fix the bug"

    def test_issues_excludes_pulls(self, client, app, monkeypatch):
        script = [
            (
                "GET",
                "/issues",
                200,
                [
                    {"number": 1, "title": "bug", "user": {"login": "alice"}, "pull_request": {}},
                    {
                        "number": 2,
                        "title": "feature",
                        "user": {"login": "bob"},
                        "labels": [{"name": "enhancement"}],
                    },
                ],
            )
        ]
        self._script_client(client, app, script, monkeypatch)
        data = client.get("/github/api/repos/owner/repo/issues").get_json()
        assert [i["number"] for i in data["items"]] == [2]
        assert data["items"][0]["labels"] == ["enhancement"]
        assert data["page"] == 1
        assert data["has_prev"] is False

    def test_pull_detail_returns_files(self, client, app, monkeypatch):
        script = [
            (
                "GET",
                "/pulls/5",
                200,
                {
                    "number": 5,
                    "title": "Add feature",
                    "state": "open",
                    "user": {"login": "alice"},
                    "additions": 10,
                    "deletions": 2,
                    "changed_files": 1,
                },
            ),
            (
                "GET",
                "/pulls/5/files",
                200,
                [{"filename": "app/x.py", "status": "modified", "patch": "diff"}],
            ),
        ]
        self._script_client(client, app, script, monkeypatch)
        data = client.get("/github/api/repos/owner/repo/pulls/5").get_json()
        assert data["number"] == 5
        assert data["files"][0]["filename"] == "app/x.py"

    def test_issues_page_envelope_exposes_navigation(self, client, app, monkeypatch):
        from app.services.github import GitHubPage

        _logged_in_client(client)
        _create_account(app)
        captured = {}

        def fake_page(self_, full_name, *, state="open", page=1, per_page=50):
            captured.update(state=state, page=page, per_page=per_page)
            return GitHubPage(
                items=[{"number": 2, "title": "feature"}],
                page=page,
                per_page=per_page,
                has_next=True,
                has_prev=False,
                total_pages=4,
            )

        monkeypatch.setattr(GitHubClient, "list_issues_page", fake_page)
        data = client.get("/github/api/repos/owner/repo/issues?state=closed&page=2").get_json()
        assert [i["number"] for i in data["items"]] == [2]
        assert data["page"] == 2
        assert data["has_next"] is True
        assert data["has_prev"] is False
        assert data["total_pages"] == 4
        assert captured["state"] == "closed"
        assert captured["page"] == 2

    def test_issues_per_page_is_capped(self, client, app, monkeypatch):
        from app.services.github import GitHubPage

        _logged_in_client(client)
        _create_account(app)
        captured = {}

        def fake_page(self_, full_name, *, state="open", page=1, per_page=50):
            captured["per_page"] = per_page
            return GitHubPage(items=[], page=page, per_page=per_page)

        monkeypatch.setattr(GitHubClient, "list_issues_page", fake_page)
        client.get("/github/api/repos/owner/repo/issues?per_page=500")
        assert captured["per_page"] == 100

    def test_pulls_page_envelope(self, client, app, monkeypatch):
        from app.services.github import GitHubPage

        _logged_in_client(client)
        _create_account(app)
        captured = {}

        def fake_page(self_, full_name, *, state="open", page=1, per_page=50):
            captured["state"] = state
            return GitHubPage(
                items=[{"number": 5, "title": "Add feature"}],
                page=page,
                per_page=per_page,
                has_next=False,
                has_prev=True,
                total_pages=2,
            )

        monkeypatch.setattr(GitHubClient, "list_pull_requests_page", fake_page)
        data = client.get("/github/api/repos/owner/repo/pulls?state=all&page=2").get_json()
        assert data["items"][0]["number"] == 5
        assert data["has_prev"] is True
        assert data["total_pages"] == 2
        assert captured["state"] == "all"

    def test_analyze_file_requires_path(self, client, app, monkeypatch):
        self._script_client(client, app, [], monkeypatch)
        response = client.post("/github/api/repos/owner/repo/analyze-file", json={"path": ""})
        assert response.status_code == 400

    def test_pages_render(self, client, app, monkeypatch):
        _logged_in_client(client)
        _create_account(app)
        assert client.get("/github/").status_code == 200
        assert client.get("/github/repos").status_code == 200
        assert client.get("/github/repos/owner/repo").status_code == 200
        assert client.get("/github/repos/owner/repo/issues").status_code == 200
        assert client.get("/github/repos/owner/repo/issues/1").status_code == 200
        assert client.get("/github/repos/owner/repo/pulls").status_code == 200
        assert client.get("/github/repos/owner/repo/pulls/1").status_code == 200


class TestSecurity:
    def test_token_never_serialized(self, client, app, monkeypatch):
        _logged_in_client(client)
        _create_account(app)
        monkeypatch.setattr(
            "app.services.github.requests.Session",
            lambda: _make_fake_session(
                [("GET", "/repos/owner/repo", 200, {"full_name": "owner/repo"})]
            ),
        )
        response = client.get("/github/api/repos/owner/repo")
        assert "gho_test_token" not in response.data.decode()
        assert "access_token_encrypted" not in response.data.decode()


def _last_session_state(client):
    with client.session_transaction() as sess:
        return sess.get("github_oauth_state")
