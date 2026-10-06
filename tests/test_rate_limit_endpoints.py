"""Tests for per-user rate limiting on the costly Phase 5 endpoints (#106).

Covers each limited endpoint (import, search, chat, stream, analyze): the first
request is allowed and subsequent requests over the limit return ``429`` with a
``Retry-After`` header. Also checks that limits are keyed per user and that
``consume`` reports a sane retry delay.
"""

from app.extensions import db
from app.models import Project, ProjectFile, Workspace
from app.models.project import SOURCE_ARCHIVE, STATUS_READY
from app.services import ratelimit


def _ready_project(make_user, login, username="rateuser", email="rateuser@example.com"):
    user = make_user(username=username, email=email)
    login(email=email)
    workspace = Workspace(user_id=user.id, name="Rate workspace")
    db.session.add(workspace)
    db.session.commit()
    project = Project(
        workspace_id=workspace.id,
        user_id=user.id,
        name="Rate project",
        source=SOURCE_ARCHIVE,
        status=STATUS_READY,
    )
    db.session.add(project)
    db.session.commit()
    db.session.add(
        ProjectFile(
            project_id=project.id,
            path="app.py",
            size=8,
            is_binary=False,
            language="python",
            content="print(1)",
        )
    )
    db.session.commit()
    return user, workspace, project


class TestImportLimit:
    def test_import_rate_limited(self, client, app, make_user, login):
        app.config["RATE_LIMIT_IMPORT_MAX"] = 1
        _, workspace, _ = _ready_project(make_user, login)
        url = f"/workspaces/api/workspaces/{workspace.id}/projects"

        # A request that fails validation still consumes the bucket.
        assert client.post(url, json={}).status_code == 400

        blocked = client.post(url, json={})
        assert blocked.status_code == 429
        assert blocked.get_json()["kind"] == "rate_limited"
        assert int(blocked.headers["Retry-After"]) >= 1


class TestSearchLimit:
    def test_search_rate_limited(self, client, app, make_user, login):
        app.config["RATE_LIMIT_SEARCH_MAX"] = 1
        _, _, project = _ready_project(make_user, login)
        url = f"/workspaces/api/projects/{project.id}/search?q=app"

        assert client.get(url).status_code == 200

        blocked = client.get(url)
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) >= 1


class TestChatLimit:
    def test_chat_rate_limited(self, client, app, make_user, login):
        app.config["RATE_LIMIT_CHAT_MAX"] = 1
        _, _, project = _ready_project(make_user, login)
        url = f"/workspaces/api/projects/{project.id}/chat"

        assert client.post(url, json={"content": "hi"}).status_code == 201

        blocked = client.post(url, json={"content": "hi"})
        assert blocked.status_code == 429
        assert blocked.get_json()["kind"] == "rate_limited"


class TestStreamLimit:
    def test_stream_rate_limited(self, client, app, make_user, login):
        app.config["RATE_LIMIT_STREAM_MAX"] = 1
        _, _, project = _ready_project(make_user, login)
        url = f"/workspaces/api/projects/{project.id}/chat/stream"

        assert client.post(url, json={"content": "hi"}).status_code == 200

        blocked = client.post(url, json={"content": "hi"})
        assert blocked.status_code == 429
        assert int(blocked.headers["Retry-After"]) >= 1


class TestAnalyzeLimit:
    def test_analyze_rate_limited(self, client, app, make_user, login):
        app.config["RATE_LIMIT_ANALYZE_MAX"] = 1
        _, _, project = _ready_project(make_user, login)
        url = f"/workspaces/api/projects/{project.id}/analyze"

        assert client.post(url, json={"kind": "architecture"}).status_code == 200

        blocked = client.post(url, json={"kind": "architecture"})
        assert blocked.status_code == 429
        assert blocked.get_json()["kind"] == "rate_limited"


class TestPerUserIsolation:
    def test_limits_are_per_user(self, client, app, make_user, login):
        app.config["RATE_LIMIT_SEARCH_MAX"] = 1
        _, _, project_one = _ready_project(make_user, login, "userone", "one@example.com")
        url_one = f"/workspaces/api/projects/{project_one.id}/search?q=app"
        assert client.get(url_one).status_code == 200
        assert client.get(url_one).status_code == 429

        # Exhausting one user's bucket must not affect another user.
        _, _, project_two = _ready_project(make_user, login, "usertwo", "two@example.com")
        url_two = f"/workspaces/api/projects/{project_two.id}/search?q=app"
        assert client.get(url_two).status_code == 200


class TestConsume:
    def test_consume_reports_retry_after(self, app):
        allowed, retry_after = ratelimit.consume("bucket", max_hits=1, window=60)
        assert allowed is True
        assert retry_after == 0

        allowed, retry_after = ratelimit.consume("bucket", max_hits=1, window=60)
        assert allowed is False
        assert 1 <= retry_after <= 60


class TestDailyCap:
    def test_daily_cap_blocks_after_limit(self, app):
        allowed, retry_after = ratelimit.daily_consume("daily-bucket", max_hits=2)
        assert allowed is True
        assert retry_after == 0

        allowed, _ = ratelimit.daily_consume("daily-bucket", max_hits=2)
        assert allowed is True

        allowed, retry_after = ratelimit.daily_consume("daily-bucket", max_hits=2)
        assert allowed is False
        assert retry_after >= 1

        assert ratelimit.daily_count("daily-bucket") == 2
        assert ratelimit.daily_remaining("daily-bucket", max_hits=2) == 0

    def test_daily_cap_disabled_when_zero(self, app):
        for _ in range(5):
            allowed, _ = ratelimit.daily_consume("unlimited-bucket", max_hits=0)
            assert allowed is True

    def test_daily_cap_counter_persists_across_calls(self, app):
        ratelimit.daily_consume("persist-bucket", max_hits=10)
        ratelimit.daily_consume("persist-bucket", max_hits=10)
        assert ratelimit.daily_count("persist-bucket") == 2

    def test_peek_reports_remaining_without_consuming(self, app):
        ratelimit.daily_consume("peek-bucket", max_hits=3)
        remaining, _reset = ratelimit.peek_daily("peek-bucket", daily_cap=3)
        assert remaining == 2
        # ``peek`` must not record a hit.
        assert ratelimit.daily_count("peek-bucket") == 1


class TestRateLimitStatusEndpoint:
    def test_status_requires_login(self, client):
        assert client.get("/api/rate-limit").status_code == 401

    def test_status_reports_daily_budget(self, client, app, make_user, login):
        app.config["RATE_LIMIT_CHAT_DAILY"] = 7
        user = make_user(username="quotauser", email="quota@example.com")
        login(email="quota@example.com")
        ratelimit.daily_consume(f"api-chat:message:user:{user.id}", max_hits=7)

        payload = client.get("/api/rate-limit").get_json()

        assert payload["daily_limit"] == 7
        assert payload["daily_remaining"] == 6
        assert payload["daily_reset_after"] >= 1
