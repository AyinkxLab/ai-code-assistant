"""Per-organization, per-family rate limits and bounded state (#621)."""

from __future__ import annotations

from app.services import ratelimit


class TestOrganizationFamilies:
    def _configure(self, app):
        app.config.update(
            API_QUOTA_TOKEN_MAX=0,
            API_QUOTA_ORG_MAX=1,
            API_QUOTA_ORG_WINDOW=60,
        )

    def test_each_family_has_its_own_organization_budget(self, client, make_user, login, app):
        make_user()
        login()
        self._configure(app)
        headers = {"X-Organization-Id": "org-1"}

        assert client.get("/api/v1/projects", headers=headers).status_code == 200
        denied = client.get("/api/v1/projects", headers=headers)
        assert denied.status_code == 429
        assert denied.get_json()["code"] == "rate_limited"
        assert client.get("/workspaces/api/workspaces", headers=headers).status_code == 200

    def test_public_v1_responses_carry_rate_limit_headers(self, client, make_user, login):
        make_user()
        login()

        response = client.get("/api/v1/projects")

        assert response.status_code == 200
        assert response.headers["X-RateLimit-Limit"] == "600"
        assert int(response.headers["X-RateLimit-Remaining"]) >= 0
        assert int(response.headers["X-RateLimit-Reset"]) >= 0


class TestBoundedState:
    def test_tracked_keys_are_capped(self, app):
        app.config["RATE_LIMIT_MAX_KEYS"] = 3

        for index in range(10):
            ratelimit.consume(f"bounded:{index}", max_hits=5, window=60)

        assert len(ratelimit._ENTRIES) <= 3

    def test_existing_key_is_updated_not_evicted(self, app):
        app.config["RATE_LIMIT_MAX_KEYS"] = 2
        ratelimit.consume("bounded:first", max_hits=5, window=60)
        ratelimit.consume("bounded:second", max_hits=5, window=60)

        allowed, remaining, _, _ = ratelimit.consume_details("bounded:first", max_hits=5, window=60)

        assert allowed is True
        assert remaining == 3
        assert len(ratelimit._ENTRIES) == 2


class TestStructuredErrors:
    def test_legacy_per_user_limiter_uses_the_catalog_body(self, client, app, make_user, login):
        make_user()
        login()
        app.config.update(RATE_LIMIT_IMPORT_MAX=1, RATE_LIMIT_IMPORT_WINDOW=60)
        url = "/workspaces/api/workspaces/1/projects"

        first = client.post(url, json={})
        assert first.status_code != 429
        blocked = client.post(url, json={})

        assert blocked.status_code == 429
        payload = blocked.get_json()
        assert payload["code"] == "rate_limited"
        assert payload["kind"] == "rate_limited"
        assert blocked.headers["Retry-After"]
