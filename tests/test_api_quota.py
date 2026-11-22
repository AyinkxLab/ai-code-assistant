"""Per-token and per-organization API quota tests (#654)."""

from types import SimpleNamespace

from app.services import ratelimit

URL = "/workspaces/api/workspaces"


def test_successful_responses_carry_rate_limit_headers(client, make_user, login):
    make_user()
    login()

    response = client.get(URL)

    assert response.status_code == 200
    assert response.headers["X-RateLimit-Limit"] == "600"
    assert int(response.headers["X-RateLimit-Remaining"]) == 599
    assert int(response.headers["X-RateLimit-Reset"]) >= 0


def test_burst_requests_are_denied_with_a_stable_body(client, make_user, login, app):
    make_user()
    login()
    app.config.update(API_QUOTA_TOKEN_MAX=2, API_QUOTA_TOKEN_WINDOW=60)
    headers = {"Authorization": "Bearer burst-token"}

    assert client.get(URL, headers=headers).status_code == 200
    assert client.get(URL, headers=headers).status_code == 200
    denied = client.get(URL, headers=headers)

    assert denied.status_code == 429
    payload = denied.get_json()
    assert payload["code"] == "rate_limited"
    assert payload["kind"] == "rate_limited"
    assert denied.headers["Retry-After"]
    assert denied.headers["X-RateLimit-Remaining"] == "0"


def test_sustained_requests_slide_with_the_window(
    client, make_user, login, app, monkeypatch
):
    make_user()
    login()
    app.config.update(API_QUOTA_TOKEN_MAX=2, API_QUOTA_TOKEN_WINDOW=60)
    clock = [1000.0]
    monkeypatch.setattr(ratelimit, "time", SimpleNamespace(monotonic=lambda: clock[0]))
    headers = {"Authorization": "Bearer sustained-token"}

    assert client.get(URL, headers=headers).status_code == 200
    clock[0] += 30
    assert client.get(URL, headers=headers).status_code == 200
    assert client.get(URL, headers=headers).status_code == 429, "burst at the second mark"
    clock[0] += 31
    assert client.get(URL, headers=headers).status_code == 200, "oldest hit left the window"


def test_organization_quota_is_enforced(client, make_user, login, app):
    make_user()
    login()
    app.config.update(
        API_QUOTA_TOKEN_MAX=100,
        API_QUOTA_ORG_MAX=1,
        API_QUOTA_ORG_WINDOW=60,
    )
    headers = {"X-Organization-Id": "42"}

    assert client.get(URL, headers=headers).status_code == 200
    denied = client.get(URL, headers=headers)

    assert denied.status_code == 429
    assert denied.get_json()["code"] == "rate_limited"


def test_zero_limit_disables_the_dimension(client, make_user, login, app):
    make_user()
    login()
    app.config.update(API_QUOTA_TOKEN_MAX=0)

    response = client.get(URL, headers={"Authorization": "Bearer unlimited"})

    assert response.status_code == 200
    assert "X-RateLimit-Limit" not in response.headers
