"""Tests for sanitized GitHub error messages (#80).

Routes must never echo GitHub's raw error body (which can contain URLs, headers,
or implementation details) or any token/header material; error kinds map to
stable user-facing strings.
"""

import json

import pytest

from app.extensions import db
from app.models import GithubAccount
from app.services.github import (
    ERROR_MESSAGES,
    GitHubClient,
    GitHubError,
    GitHubPermissionError,
    github_error_payload,
)

SECRET_MARKER = "ghp_SUPER_SECRET_TOKEN_123"


class FakeResponse:
    def __init__(self, status_code=200, data=None, text="", headers=None):
        self.status_code = status_code
        self._data = data
        self.text = text
        self.headers = headers or {}
        self.content = (text or (json.dumps(data) if data is not None else "")).encode()

    def json(self):
        return self._data


class FakeSession:
    def __init__(self, response):
        self.response = response
        self.headers = {}

    def request(self, method, url, params=None, timeout=None, **kwargs):
        return self.response

    def get(self, url, params=None, timeout=None, **kwargs):
        return self.request("GET", url, params=params, timeout=timeout)


class TestErrorMessageMap:
    def test_all_kinds_have_a_stable_message_without_secrets(self):
        for kind in (
            "not_connected",
            "auth",
            "permission",
            "not_found",
            "rate_limit",
            "network",
            "validation",
            "github_error",
        ):
            assert kind in ERROR_MESSAGES
            assert SECRET_MARKER not in ERROR_MESSAGES[kind]

    def test_payload_uses_stable_message_not_raw_detail(self):
        exc = GitHubPermissionError(
            "raw github said: " + SECRET_MARKER, detail="x-access-token " + SECRET_MARKER
        )
        payload = github_error_payload(exc)
        assert payload == {"error": ERROR_MESSAGES["permission"], "kind": "permission"}
        assert SECRET_MARKER not in json.dumps(payload)


class TestClientSanitization:
    def test_permission_error_hides_raw_body(self, app, monkeypatch):
        response = FakeResponse(
            403,
            data={"message": f"Bad credentials {SECRET_MARKER}"},
            headers={
                "X-RateLimit-Remaining": "42",
                "Set-Cookie": f"token={SECRET_MARKER}",
            },
        )
        monkeypatch.setattr("app.services.github.requests.Session", lambda: FakeSession(response))

        with pytest.raises(GitHubPermissionError) as excinfo:
            GitHubClient("gho_test").get_user()

        payload = github_error_payload(excinfo.value)
        assert SECRET_MARKER not in json.dumps(payload)
        assert payload["error"] == ERROR_MESSAGES["permission"]
        # The raw detail is retained server-side only (for logging).
        assert SECRET_MARKER in (excinfo.value.detail or "")

    def test_generic_error_hides_raw_body(self, app, monkeypatch):
        response = FakeResponse(422, data={"message": f"Validation failed {SECRET_MARKER}"})
        monkeypatch.setattr("app.services.github.requests.Session", lambda: FakeSession(response))

        with pytest.raises(GitHubError) as excinfo:
            GitHubClient("gho_test").get_user()

        assert SECRET_MARKER not in json.dumps(github_error_payload(excinfo.value))


def _login_with_github_account(client, make_user, login):
    user = make_user(username="ghsec", email="ghsec@example.com")
    login(email="ghsec@example.com")
    account = GithubAccount(user_id=user.id, github_user_id=1, github_username="ghsec")
    account.set_access_token("gho_test")
    db.session.add(account)
    db.session.commit()


class TestRouteSanitization:
    def test_repos_error_is_sanitized(self, client, app, make_user, login, monkeypatch):
        _login_with_github_account(client, make_user, login)

        def boom():
            raise GitHubPermissionError(
                f"raw github message {SECRET_MARKER}", detail=f"header {SECRET_MARKER}"
            )

        monkeypatch.setattr("app.github.routes.get_github_client", boom)
        response = client.get("/github/api/repos")

        assert response.status_code == 403
        assert SECRET_MARKER not in response.get_data(as_text=True)
        body = response.get_json()
        assert body["error"] == ERROR_MESSAGES["permission"]
        assert body["kind"] == "permission"

    def test_repo_detail_not_found_is_stable(self, client, app, make_user, login, monkeypatch):
        _login_with_github_account(client, make_user, login)

        from app.services.github import GitHubNotFoundError

        def boom(self_, full_name):
            raise GitHubNotFoundError(f"resource {SECRET_MARKER}")

        monkeypatch.setattr(GitHubClient, "get_repository", boom)
        response = client.get("/github/api/repos/owner/missing")

        assert response.status_code == 404
        assert SECRET_MARKER not in response.get_data(as_text=True)
        assert response.get_json()["kind"] == "not_found"
