"""Tests for OAuth callback throttling (#81).

The ``/github/callback`` endpoint validates the ``state`` parameter but should
also throttle repeated *failures* so an attacker cannot brute-force state
probing. Only failed attempts are counted and a successful connection clears the
bucket, so legitimate connects are never affected.
"""

import json

from app.models import GithubAccount, User


class FakeResponse:
    def __init__(self, status_code=200, data=None, text="", headers=None):
        self.status_code = status_code
        self._data = data
        self.text = text
        self.headers = headers or {}
        self.content = (text or json.dumps(data) if data is not None else "").encode()

    def json(self):
        return self._data


def _user_session(login):
    class FakeSession:
        def __init__(self):
            self.headers = {}

        def get(self, url, params=None, timeout=None, **kwargs):
            return FakeResponse(200, {"id": 42, "login": login})

        def request(self, method, url, params=None, timeout=None, **kwargs):
            return self.get(url, params=params, timeout=timeout)

    return FakeSession()


def _failed_callback(client):
    """Trigger a state-mismatch failure without following the redirect."""
    return client.get("/github/callback?code=abc&state=attacker-supplied")


def _session_state(client):
    with client.session_transaction() as sess:
        return sess.get("github_oauth_state")


class TestCallbackThrottle:
    def test_repeated_failures_are_throttled(self, client, app, make_user, login):
        app.config["RATE_LIMIT_OAUTH_CALLBACK_MAX"] = 2
        app.config["RATE_LIMIT_OAUTH_CALLBACK_WINDOW"] = 300
        make_user(username="ghuser", email="ghuser@example.com")
        login(email="ghuser@example.com")

        # The first failures are allowed (and redirect back to the dashboard).
        assert _failed_callback(client).status_code == 302
        assert _failed_callback(client).status_code == 302

        blocked = _failed_callback(client)
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) >= 1
        assert b"Too many failed GitHub authorization attempts" in blocked.data

    def test_limit_is_keyed_per_user(self, client, app, make_user, login):
        app.config["RATE_LIMIT_OAUTH_CALLBACK_MAX"] = 1
        make_user(username="first", email="first@example.com")
        make_user(username="second", email="second@example.com")

        login(email="first@example.com")
        assert _failed_callback(client).status_code == 302
        assert _failed_callback(client).status_code == 429

        # A different user has an independent bucket.
        login(email="second@example.com")
        assert _failed_callback(client).status_code == 302

    def test_successful_connect_clears_failure_bucket(
        self, client, app, make_user, login, monkeypatch
    ):
        app.config["RATE_LIMIT_OAUTH_CALLBACK_MAX"] = 2
        app.config["GITHUB_CLIENT_ID"] = "client-id"
        app.config["GITHUB_CLIENT_SECRET"] = "client-secret"
        make_user(username="ghuser", email="ghuser@example.com")
        login(email="ghuser@example.com")

        # Burn one failure, then complete a legitimate connection.
        assert _failed_callback(client).status_code == 302

        client.get("/github/connect")
        state = _session_state(client)
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
        monkeypatch.setattr("app.services.github.requests.Session", lambda: _user_session("ghuser"))

        response = client.get(f"/github/callback?code=abc&state={state}", follow_redirects=True)
        assert b"Connected to GitHub as" in response.data

        # The success cleared the bucket: the next failure is allowed again.
        assert _failed_callback(client).status_code == 302

        user = User.query.filter_by(username="ghuser").first()
        assert GithubAccount.query.filter_by(user_id=user.id).first() is not None
