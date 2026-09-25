"""Tests for the GitHub API service layer.

The GitHub client is tested against a fake ``requests.Session`` so no real
network calls are made. The fake verifies request paths/params and returns
scripted responses (success, rate limit, 404, 5xx, network failure).
"""

import json

import pytest

from app.services.github import (
    PAGE_SIZE_MAX,
    GitHubAuthError,
    GitHubClient,
    GitHubError,
    GitHubNetworkError,
    GitHubNotFoundError,
    GitHubPage,
    GitHubPermissionError,
    GitHubRateLimitError,
    parse_link_header,
    validate_full_name,
    validate_path,
)


class FakeResponse:
    def __init__(self, status_code=200, data=None, text="", headers=None, content=b""):
        self.status_code = status_code
        self._data = data
        self.text = text
        self.headers = headers or {}
        self.content = content

    def json(self):
        return self._data

    @classmethod
    def from_json(cls, status_code, data, headers=None):
        return cls(status_code, data=data, content=json.dumps(data).encode(), headers=headers)


class FakeSession:
    """Records requests and returns scripted responses."""

    def __init__(self, responses=None):
        self.responses = list(responses or [])
        self.calls = []
        self.headers = {}

    def _next(self):
        if len(self.responses) == 1:
            return self.responses[0]
        if not self.responses:
            raise AssertionError("No more scripted responses")
        return self.responses.pop(0)

    def request(self, method, url, params=None, timeout=None):
        self.calls.append({"method": method, "url": url, "params": params, "timeout": timeout})
        response = self._next()
        return response

    def get(self, url, params=None, timeout=None):
        return self.request("GET", url, params=params, timeout=timeout)


@pytest.fixture()
def ok_client(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr("app.services.github.requests.Session", lambda: session)
    return GitHubClient("gho_test_token"), session


def _repo_dict(name="owner/repo"):
    return {
        "full_name": name,
        "name": name.split("/")[1],
        "description": "A test repo",
        "owner": {"login": "owner"},
        "visibility": "public",
        "private": False,
        "default_branch": "main",
        "language": "Python",
        "updated_at": "2026-01-01T00:00:00Z",
        "html_url": f"https://github.com/{name}",
        "size": 10,
        "fork": False,
        "pushed_at": "2026-01-02T00:00:00Z",
    }


class TestRequestHandling:
    def test_get_sends_auth_and_version_headers(self, ok_client):
        client, session = ok_client
        session.responses = [FakeResponse.from_json(200, {"id": 1})]
        client.get_user()
        assert session.headers["Authorization"] == "Bearer gho_test_token"
        assert session.headers["X-GitHub-Api-Version"] == "2022-11-28"

    def test_not_found_raises_typed_error(self, ok_client):
        client, session = ok_client
        session.responses = [FakeResponse(404, data={"message": "Not Found"})]
        with pytest.raises(GitHubNotFoundError):
            client.get_repository("owner/missing")

    def test_unauthorized_raises_auth_error(self, ok_client):
        client, session = ok_client
        session.responses = [FakeResponse(401, data={"message": "Bad credentials"})]
        with pytest.raises(GitHubAuthError):
            client.get_user()

    def test_rate_limit_raises_rate_limit_error(self, ok_client):
        client, session = ok_client
        session.responses = [
            FakeResponse(
                403,
                data={"message": "API rate limit exceeded"},
                headers={"X-RateLimit-Remaining": "0", "X-RateLimit-Reset": str(1_700_000_000)},
            )
        ]
        with pytest.raises(GitHubRateLimitError):
            client.get_user()

    def test_forbidden_with_quota_raises_permission_error(self, ok_client):
        client, session = ok_client
        session.responses = [
            FakeResponse(403, data={"message": "Resource not accessible by integration"})
        ]
        with pytest.raises(GitHubPermissionError):
            client.get_user()

    def test_server_error_is_retried_then_raises(self, ok_client):
        client, session = ok_client
        session.responses = [
            FakeResponse(500, data={}),
            FakeResponse(503, data={}),
            FakeResponse(502, data={}),
        ]
        with pytest.raises(GitHubNetworkError):
            client.get_user()
        assert len(session.calls) == 3

    def test_network_error_is_retried_then_raises(self, ok_client, monkeypatch):
        import requests as req

        client, session = ok_client

        def boom(*args, **kwargs):
            raise req.exceptions.ConnectionError("boom")

        monkeypatch.setattr(session, "request", boom)
        with pytest.raises(GitHubNetworkError):
            client.get_user()


class TestRepositoryMethods:
    def test_list_repositories_paginates(self, ok_client):
        client, session = ok_client
        session.responses = [
            FakeResponse(
                200,
                data=[_repo_dict("owner/one")],
                headers={"Link": '</repos?page=2>; rel="next"'},
            ),
            FakeResponse(200, data=[_repo_dict("owner/two")]),
        ]
        repos = client.list_repositories()
        assert [r["full_name"] for r in repos] == ["owner/one", "owner/two"]

    def test_get_file_text_decodes_base64(self, ok_client):
        import base64 as b64

        client, session = ok_client
        content = b64.b64encode(b"print('hello')\n").decode()
        session.responses = [
            FakeResponse(
                200,
                data={"type": "file", "path": "main.py", "content": content},
                headers={"Content-Type": "application/json; charset=utf-8"},
            )
        ]
        assert client.get_file_text("owner/repo", "main.py") == "print('hello')\n"

    def test_list_issues_excludes_pull_requests(self, ok_client):
        client, session = ok_client
        session.responses = [
            FakeResponse.from_json(
                200,
                [
                    {"number": 1, "title": "bug", "pull_request": {}},
                    {"number": 2, "title": "feature"},
                ],
            )
        ]
        issues = client.list_issues("owner/repo")
        assert [i["number"] for i in issues] == [2]


class TestPagination:
    def test_parse_link_header(self):
        header = (
            '<https://api.github.com/repos/o/r/issues?page=3>; rel="next", '
            '<https://api.github.com/repos/o/r/issues?page=1>; rel="prev", '
            '<https://api.github.com/repos/o/r/issues?page=7>; rel="last"'
        )
        links = parse_link_header(header)
        assert links["next"].endswith("page=3")
        assert links["prev"].endswith("page=1")
        assert links["last"].endswith("page=7")
        assert parse_link_header("") == {}

    def test_issues_page_reports_navigation_from_link_header(self, ok_client):
        client, session = ok_client
        session.responses = [
            FakeResponse.from_json(
                200,
                [{"number": 2, "title": "feature"}],
                headers={
                    "Link": (
                        '<https://api.github.com/repos/o/r/issues?page=2>; rel="next", '
                        '<https://api.github.com/repos/o/r/issues?page=9>; rel="last"'
                    )
                },
            )
        ]
        page = client.list_issues_page("owner/repo", state="open", page=1, per_page=50)
        assert isinstance(page, GitHubPage)
        assert [i["number"] for i in page.items] == [2]
        assert page.page == 1
        assert page.per_page == 50
        assert page.has_next is True
        assert page.has_prev is False
        assert page.total_pages == 9

    def test_issues_page_excludes_pull_requests(self, ok_client):
        client, session = ok_client
        session.responses = [
            FakeResponse.from_json(
                200,
                [
                    {"number": 1, "title": "pr", "pull_request": {}},
                    {"number": 2, "title": "issue"},
                ],
            )
        ]
        page = client.list_issues_page("owner/repo")
        assert [i["number"] for i in page.items] == [2]

    def test_pulls_page_passes_page_and_clamps_per_page(self, ok_client):
        client, session = ok_client
        session.responses = [FakeResponse.from_json(200, [{"number": 5, "title": "pr"}])]
        page = client.list_pull_requests_page(
            "owner/repo", state="closed", page=3, per_page=PAGE_SIZE_MAX + 100
        )
        assert page.per_page == PAGE_SIZE_MAX
        assert page.page == 3
        assert session.calls[0]["params"]["page"] == 3
        assert session.calls[0]["params"]["per_page"] == PAGE_SIZE_MAX
        assert session.calls[0]["params"]["state"] == "closed"

    def test_page_without_next_has_no_total(self, ok_client):
        client, session = ok_client
        session.responses = [FakeResponse.from_json(200, [{"number": 1}])]
        page = client.list_pull_requests_page("owner/repo")
        assert page.has_next is False
        assert page.has_prev is False
        assert page.total_pages == 1

    def test_page_error_is_translated_to_typed_error(self, ok_client):
        client, session = ok_client
        session.responses = [
            FakeResponse(404, data={"message": "Not Found"}),
            FakeResponse(404, data={"message": "Not Found"}),
        ]
        with pytest.raises(GitHubNotFoundError):
            client.list_issues_page("owner/repo")


class TestRateLimit:
    def test_get_rate_limit_normalizes_core(self, ok_client):
        client, session = ok_client
        session.responses = [
            FakeResponse.from_json(
                200,
                {
                    "resources": {
                        "core": {
                            "limit": 5000,
                            "remaining": 12,
                            "reset": 1_700_000_000,
                            "used": 4988,
                        }
                    },
                    "rate": {"limit": 5000, "remaining": 12, "reset": 1_700_000_000},
                },
            )
        ]
        budget = client.get_rate_limit()
        assert budget == {
            "limit": 5000,
            "remaining": 12,
            "reset": 1_700_000_000,
            "used": 4988,
        }
        assert session.calls[0]["url"].endswith("/rate_limit")

    def test_get_rate_limit_falls_back_to_rate_object(self, ok_client):
        client, session = ok_client
        session.responses = [
            FakeResponse.from_json(
                200, {"rate": {"limit": 60, "remaining": 60, "reset": 1_700_000_000}}
            )
        ]
        assert client.get_rate_limit() == {
            "limit": 60,
            "remaining": 60,
            "reset": 1_700_000_000,
            "used": None,
        }

    def test_get_rate_limit_returns_none_when_missing(self, ok_client):
        client, session = ok_client
        session.responses = [FakeResponse.from_json(200, {"resources": {}})]
        assert client.get_rate_limit() is None

    def test_get_rate_limit_returns_none_on_non_dict(self, ok_client):
        client, session = ok_client
        session.responses = [FakeResponse.from_json(200, [])]
        assert client.get_rate_limit() is None


class TestValidation:
    def test_validate_full_name_accepts_valid(self):
        assert validate_full_name("owner/repo") == "owner/repo"
        assert validate_full_name("  Owner_1.2/My-Repo.3  ") == "Owner_1.2/My-Repo.3"

    def test_validate_full_name_rejects_bad(self):
        for bad in ["", "norepo", "a/b/c", "a b/c", "a/b c", "../evil/repo"]:
            with pytest.raises(GitHubError):
                validate_full_name(bad)

    def test_validate_path_rejects_traversal(self):
        assert validate_path("/foo/bar.txt") == "foo/bar.txt"
        for bad in ["../secret", "foo/../../etc/passwd", "..", "a/../b"]:
            with pytest.raises(GitHubError):
                validate_path(bad)
