"""Tests for the bounded GitHub ETag / conditional-request cache (issue #76).

The client is exercised against a fake ``requests.Session`` backed by a minimal
GitHub stand-in that honours ``If-None-Match`` the way the real API does: a
matching validator yields ``304`` with an empty body, anything else yields
``200`` with the full body and an ``ETag``. No real network calls are made.
"""

import base64
import json

import pytest

from app.services.github import GitHubClient
from app.services.github_cache import (
    CachedSession,
    GitHubResponseCache,
    auth_fingerprint,
    cache_key,
    is_cacheable_url,
)

API = "https://api.github.com"
REPOS_URL = f"{API}/user/repos"
README_URL = f"{API}/repos/owner/repo/readme"
USER_URL = f"{API}/user"


class FakeResponse:
    """Stand-in for ``requests.Response`` with a settable ``content``."""

    def __init__(self, status_code=200, content=b"", headers=None):
        self.status_code = status_code
        self.headers = dict(headers or {})
        self._content = content

    @property
    def content(self):
        return self._content

    @content.setter
    def content(self, value):
        self._content = value

    @property
    def text(self):
        return self._content.decode("utf-8", "replace")

    def json(self):
        if not self._content:
            # Real ``requests`` raises JSONDecodeError (a ValueError) here.
            raise ValueError("No JSON object could be decoded")
        return json.loads(self._content)


class FakeGitHub:
    """Holds response bodies per URL and answers with real ETag semantics."""

    def __init__(self):
        self.resources: dict[str, tuple[bytes, dict[str, str]]] = {}
        self.calls: list[dict] = []
        #: URLs that answer 304 even when no validator was sent, to simulate a
        #: stray/hostile upstream response.
        self.stale: set[str] = set()

    def store(self, url, payload, *, etag='"v1"', headers=None):
        """Register (or update) the current JSON representation of ``url``."""
        return self.store_raw(url, json.dumps(payload).encode(), etag=etag, headers=headers)

    def store_raw(self, url, body, *, etag='"v1"', headers=None):
        """Register a non-JSON body (e.g. a README fetched with the raw Accept)."""
        response_headers = {"ETag": etag, "Content-Type": "application/json"}
        response_headers.update(headers or {})
        self.resources[url] = (body, response_headers)
        return etag

    def handle(self, method, url, params, headers):
        headers = headers or {}
        self.calls.append(
            {
                "method": method,
                "url": url,
                "params": params,
                "if_none_match": headers.get("If-None-Match"),
            }
        )
        if method != "GET":
            return FakeResponse(204)

        resource = self.resources.get(url)
        if resource is None and url not in self.stale:
            return FakeResponse(404, json.dumps({"message": "Not Found"}).encode())

        if url in self.stale:
            return FakeResponse(304, b"")

        body, response_headers = resource
        # Only a validator that was actually sent can match; comparing against a
        # missing header would make "no If-None-Match" equal "no ETag" and hand
        # back a 304 for a first-ever request.
        validator = headers.get("If-None-Match")
        if validator is not None and validator == response_headers.get("ETag"):
            # GitHub sends 304 with no body at all.
            return FakeResponse(304, b"")
        return FakeResponse(200, body, dict(response_headers))

    @property
    def get_calls(self):
        return [call for call in self.calls if call["method"] == "GET"]


class FakeSession:
    """Records requests and delegates to a :class:`FakeGitHub`."""

    def __init__(self, github):
        self.github = github
        self.headers: dict[str, str] = {}

    def request(self, method, url, *, params=None, headers=None, timeout=None):
        return self.github.handle(method, url, params, headers)

    def get(self, url, params=None, headers=None, timeout=None):
        return self.request("GET", url, params=params, headers=headers, timeout=timeout)


class Clock:
    """Manually advanced monotonic clock for TTL assertions."""

    def __init__(self):
        self.now = 1000.0

    def __call__(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


def _client(monkeypatch, github, token="gho_user_a", **cache_kwargs):
    session = FakeSession(github)
    monkeypatch.setattr("app.services.github.requests.Session", lambda: session)
    cache = GitHubResponseCache(**cache_kwargs)
    return GitHubClient(token, cache=cache), cache


def _repo(name):
    return {
        "full_name": name,
        "name": name.split("/")[1],
        "private": False,
        "default_branch": "main",
        "owner": {"login": name.split("/")[0]},
    }


class TestRevalidationReusesCachedBody:
    """Acceptance criterion: 304 responses reuse the cached body."""

    def test_second_call_replays_body_and_sends_validator(self, monkeypatch):
        github = FakeGitHub()
        etag = github.store(REPOS_URL, [_repo("owner/repo")])
        client, _ = _client(monkeypatch, github)

        first = client.list_repositories()
        second = client.list_repositories()

        assert len(github.get_calls) == 2, "a conditional request must still be made"
        assert github.get_calls[1]["if_none_match"] == etag
        assert second == first, "304 must replay the cached body, not an empty one"
        assert second[0]["full_name"] == "owner/repo"

    def test_304_does_not_download_the_body_again(self, monkeypatch):
        github = FakeGitHub()
        github.store(REPOS_URL, [_repo("owner/repo")])
        client, _ = _client(monkeypatch, github)

        client.list_repositories()
        client.list_repositories()

        # The stand-in only records calls it was asked to serve; a 304 proves the
        # second response carried no payload at all.
        assert github.get_calls[0]["if_none_match"] is None
        assert github.get_calls[1]["if_none_match"] == '"v1"'

    def test_readme_and_tree_reuse_cached_body(self, monkeypatch):
        github = FakeGitHub()
        blob = base64.b64encode(b"print('hi')").decode()
        # The README is fetched with Accept: application/vnd.github.raw+json, so
        # GitHub returns plain text rather than the base64 JSON envelope.
        github.store_raw(README_URL, b"# Title")
        github.store(
            f"{API}/repos/owner/repo/contents/app.py",
            {"content": blob, "encoding": "base64"},
        )
        client, _ = _client(monkeypatch, github)

        assert client.get_readme("owner/repo") == "# Title"
        assert client.get_readme("owner/repo") == "# Title"
        assert client.get_file_text("owner/repo", "app.py") == "print('hi')"
        assert client.get_file_text("owner/repo", "app.py") == "print('hi')"

        assert [call["if_none_match"] for call in github.get_calls] == [
            None,
            '"v1"',
            None,
            '"v1"',
        ]

    def test_replayed_response_keeps_link_header_for_pagination(self, monkeypatch):
        """Pagination parses ``Link``; the replay must not lose it."""
        github = FakeGitHub()
        github.store(
            f"{API}/repos/owner/repo/issues",
            [{"number": 1, "pull_request": None}],
            headers={"Link": f'<{API}/repos/owner/repo/issues?page=2>; rel="next"'},
        )
        client, cache = _client(monkeypatch, github)

        first = client.list_issues_page("owner/repo", per_page=1)
        second = client.list_issues_page("owner/repo", per_page=1)

        assert first.has_next is True
        assert second.has_next is True, "Link header was lost when replaying the 304"
        assert second.items == first.items
        assert cache.stats()["revalidations"] == 1

    def test_changed_etag_refetches_fresh_body(self, monkeypatch):
        github = FakeGitHub()
        github.store(REPOS_URL, [_repo("owner/old")], etag='"v1"')
        client, _ = _client(monkeypatch, github)

        assert client.list_repositories()[0]["full_name"] == "owner/old"

        github.store(REPOS_URL, [_repo("owner/new")], etag='"v2"')
        assert client.list_repositories()[0]["full_name"] == "owner/new"

    def test_response_without_etag_is_not_cached(self, monkeypatch):
        github = FakeGitHub()
        github.store(REPOS_URL, [_repo("owner/repo")], etag=None)
        client, cache = _client(monkeypatch, github)

        client.list_repositories()
        client.list_repositories()

        assert cache.stats()["sets"] == 0
        assert [call["if_none_match"] for call in github.get_calls] == [None, None]

    def test_no_store_cache_control_is_not_cached(self, monkeypatch):
        github = FakeGitHub()
        github.store(REPOS_URL, [_repo("owner/repo")], headers={"Cache-Control": "no-store"})
        client, cache = _client(monkeypatch, github)

        client.list_repositories()
        client.list_repositories()

        assert cache.stats()["sets"] == 0
        assert github.get_calls[1]["if_none_match"] is None

    def test_non_get_requests_are_not_cached(self, monkeypatch):
        github = FakeGitHub()
        client, cache = _client(monkeypatch, github)

        client._request("POST", "/user/repos")
        client._request("POST", "/user/repos")

        assert cache.stats()["sets"] == 0
        assert github.calls[0]["if_none_match"] is None
        assert github.calls[1]["if_none_match"] is None

    def test_user_endpoint_is_not_cached(self, monkeypatch):
        """``/user`` is an authorization probe; a stale copy would hide revocation."""
        github = FakeGitHub()
        github.store(USER_URL, {"login": "user_a"})
        client, cache = _client(monkeypatch, github)

        client.get_user()
        client.get_user()

        assert cache.stats()["sets"] == 0
        assert github.get_calls[1]["if_none_match"] is None

    def test_param_order_does_not_create_a_second_entry(self, monkeypatch):
        github = FakeGitHub()
        github.store(f"{API}/x", {"ok": True})
        cache = GitHubResponseCache()
        session = CachedSession(FakeSession(github), cache, auth_fingerprint("t"))

        session.request("GET", f"{API}/x", params={"b": 2, "a": 1})
        session.request("GET", f"{API}/x", params={"a": 1, "b": 2})

        assert cache.stats()["sets"] == 1
        assert github.get_calls[1]["if_none_match"] == '"v1"'


class TestCacheIsBounded:
    """Acceptance criterion: the cache has a size and TTL bound."""

    def test_max_entries_evicts_least_recently_used(self, monkeypatch):
        github = FakeGitHub()
        github.store(f"{API}/a", {"v": 1})
        github.store(f"{API}/b", {"v": 2})
        github.store(f"{API}/c", {"v": 3})
        client, cache = _client(monkeypatch, github, max_entries=2)

        for path in ("a", "b", "c"):
            client._get(f"/{path}")

        stats = cache.stats()
        assert stats["entries"] == 2, "store grew past its configured bound"
        assert stats["evictions"] == 1

        # "a" was the least recently used, so it must have been dropped and is
        # now re-fetched in full rather than revalidated.
        before = len(github.get_calls)
        client._get("/a")
        assert github.get_calls[before]["if_none_match"] is None

        # "c" is still cached and is revalidated.
        before = len(github.get_calls)
        client._get("/c")
        assert github.get_calls[before]["if_none_match"] == '"v1"'

    def test_ttl_expires_entries(self, monkeypatch):
        github = FakeGitHub()
        github.store(REPOS_URL, [_repo("owner/repo")])
        clock = Clock()
        client, cache = _client(monkeypatch, github, ttl=60, clock=clock)

        client.list_repositories()
        client.list_repositories()
        assert github.get_calls[1]["if_none_match"] == '"v1"'

        clock.advance(61)
        client.list_repositories()

        assert github.get_calls[2]["if_none_match"] is None, "expired entry was reused"
        assert cache.stats()["expirations"] == 1

    def test_entry_within_ttl_is_still_reused(self, monkeypatch):
        github = FakeGitHub()
        github.store(REPOS_URL, [_repo("owner/repo")])
        clock = Clock()
        client, cache = _client(monkeypatch, github, ttl=60, clock=clock)

        client.list_repositories()
        clock.advance(59)
        client.list_repositories()

        assert github.get_calls[1]["if_none_match"] == '"v1"'
        assert cache.stats()["expirations"] == 0

    def test_oversized_body_is_not_stored(self, monkeypatch):
        github = FakeGitHub()
        github.store(REPOS_URL, [{"blob": "x" * 500}])
        client, cache = _client(monkeypatch, github, max_body_bytes=64)

        client.list_repositories()
        client.list_repositories()

        assert cache.stats()["sets"] == 0
        # Both calls attempt (and decline) a store, since neither is cacheable.
        assert cache.stats()["oversized"] == 2
        assert cache.stats()["entries"] == 0
        assert github.get_calls[1]["if_none_match"] is None

    def test_disabled_cache_passes_everything_through(self, monkeypatch):
        github = FakeGitHub()
        github.store(REPOS_URL, [_repo("owner/repo")])
        client, cache = _client(monkeypatch, github, enabled=False)

        assert client.list_repositories() == client.list_repositories()
        assert cache.stats()["sets"] == 0
        assert [call["if_none_match"] for call in github.get_calls] == [None, None]

    def test_clear_empties_the_store(self, monkeypatch):
        github = FakeGitHub()
        github.store(REPOS_URL, [_repo("owner/repo")])
        client, cache = _client(monkeypatch, github)

        client.list_repositories()
        assert cache.stats()["entries"] == 1
        cache.clear()
        assert cache.stats()["entries"] == 0


class TestCacheIsScopedToAuthContext:
    """Acceptance criterion: cache keys include the auth context (per-user)."""

    def test_other_token_never_receives_a_cached_body(self, monkeypatch):
        """The isolation guarantee: user B must not see user A's private data."""
        github = FakeGitHub()
        github.store(REPOS_URL, [_repo("private-org/secret")])
        cache = GitHubResponseCache()
        session = FakeSession(github)

        monkeypatch.setattr("app.services.github.requests.Session", lambda: session)
        user_a = GitHubClient("gho_alice", cache=cache)
        user_b = GitHubClient("gho_bob", cache=cache)

        first = user_a.list_repositories()
        second = user_b.list_repositories()

        assert first[0]["full_name"] == "private-org/secret"
        assert second[0]["full_name"] == "private-org/secret"
        assert github.get_calls[1]["if_none_match"] is None, "token B revalidated A's entry"

    def test_same_token_shares_the_cache(self, monkeypatch):
        github = FakeGitHub()
        github.store(REPOS_URL, [_repo("owner/repo")])
        cache = GitHubResponseCache()

        monkeypatch.setattr("app.services.github.requests.Session", lambda: FakeSession(github))
        user_a = GitHubClient("gho_alice", cache=cache)
        user_a_again = GitHubClient("gho_alice", cache=cache)

        user_a.list_repositories()
        user_a_again.list_repositories()

        assert github.get_calls[1]["if_none_match"] == '"v1"'

    def test_cache_key_differs_per_auth_fingerprint(self):
        shared = {"method": "GET", "url": REPOS_URL, "params": {"page": 1}}
        assert cache_key(auth_fingerprint("token_a"), **shared) != cache_key(
            auth_fingerprint("token_b"), **shared
        )
        assert cache_key(auth_fingerprint("token_a"), **shared) == cache_key(
            auth_fingerprint("token_a"), **shared
        )

    def test_cache_key_does_not_contain_the_raw_token(self):
        key = cache_key(auth_fingerprint("gho_super_secret"), "GET", REPOS_URL)
        assert "gho_super_secret" not in key

    def test_invalidate_auth_drops_only_that_account(self, monkeypatch):
        github = FakeGitHub()
        github.store(f"{API}/a", {"v": 1})
        github.store(f"{API}/b", {"v": 2})
        cache = GitHubResponseCache()

        monkeypatch.setattr("app.services.github.requests.Session", lambda: FakeSession(github))
        alice = GitHubClient("gho_alice", cache=cache)
        bob = GitHubClient("gho_bob", cache=cache)
        alice._get("/a")
        bob._get("/a")
        bob._get("/b")
        assert cache.stats()["entries"] == 3

        removed = cache.invalidate_auth(auth_fingerprint("gho_alice"))

        assert removed == 1
        assert cache.stats()["entries"] == 2
        before = len(github.get_calls)
        alice._get("/a")
        bob._get("/a")
        assert github.get_calls[before]["if_none_match"] is None, "revoked token entry survived"
        assert github.get_calls[before + 1]["if_none_match"] == '"v1"'

    def test_304_without_a_cached_entry_does_not_fabricate_data(self, monkeypatch):
        """A stray 304 must surface as-is, never as invented content."""
        github = FakeGitHub()
        client, cache = _client(monkeypatch, github)

        # Nothing was ever cached for this URL, so the 304 cannot be replayed and
        # must be passed through untouched rather than turned into invented data.
        github.stale.add(f"{API}/never-fetched")
        response = client.session.request("GET", f"{API}/never-fetched")
        assert response.status_code == 304
        assert response.content == b""
        assert cache.stats()["entries"] == 0

    @pytest.mark.parametrize(
        ("url", "expected"),
        [
            (f"{API}/user", False),
            (f"{API}/user/", False),
            (f"{API}/api/v3/user", False),
            (REPOS_URL, True),
            (README_URL, True),
            (f"{API}/users/octocat", True),
        ],
    )
    def test_is_cacheable_url(self, url, expected):
        assert is_cacheable_url(url) is expected
