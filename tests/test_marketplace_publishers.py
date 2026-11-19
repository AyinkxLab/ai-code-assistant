"""Publisher profile pages and trusted-key badges (#691)."""

import base64

from app.extensions import db
from app.models import Plugin
from app.services.marketplace_analytics import record_plugin_event

KEY = base64.b64encode(b"\x01" * 32).decode()


def _plugin(plugin_id, *, author="Acme Labs", trust_publisher=None, trust_state="Unverified"):
    plugin = Plugin(
        id=plugin_id,
        name=plugin_id.replace("-", " ").title(),
        version="1.0.0",
        description=f"{plugin_id} description",
        author=author,
        entry_point="plugins.test:TestPlugin",
        capabilities=["PROJECT_READ"],
        trust_state=trust_state,
        trust_publisher=trust_publisher,
    )
    db.session.add(plugin)
    db.session.commit()
    return plugin


def _trust(app):
    app.config["PLUGIN_TRUSTED_KEYS"] = {"acme-labs": KEY}


class TestPublisherProfileApi:
    def test_verified_publisher_shows_keys_plugins_and_install_counts(
        self, app, client, make_user, login
    ):
        make_user()
        login()
        _trust(app)
        _plugin("alpha", trust_publisher="acme-labs", trust_state="Trusted")
        _plugin("beta", trust_publisher="acme-labs", trust_state="Trusted")
        record_plugin_event("alpha", "install")

        payload = client.get("/plugins/api/publishers/acme-labs").get_json()

        assert payload["publisher"] == {
            "id": "acme-labs",
            "name": "Acme Labs",
            "verified": True,
            "badge": "verified",
            "verified_key_ids": ["acme-labs"],
        }
        assert [plugin["id"] for plugin in payload["plugins"]] == ["alpha", "beta"]
        assert payload["plugins"][0]["installs"] == 1
        assert payload["pagination"]["total"] == 2

    def test_unverified_author_is_slugged_and_has_no_badge(self, app, client, make_user, login):
        make_user()
        login()
        _plugin("solo", author="Solo Dev", trust_state="Unverified")

        payload = client.get("/plugins/api/publishers/solo-dev").get_json()

        assert payload["publisher"]["verified"] is False
        assert payload["publisher"]["verified_key_ids"] == []
        assert [plugin["id"] for plugin in payload["plugins"]] == ["solo"]

    def test_pagination_is_bounded_and_stable(self, app, client, make_user, login):
        make_user()
        login()
        _plugin("alpha", trust_publisher="acme-labs")
        _plugin("beta", trust_publisher="acme-labs")
        _plugin("gamma", trust_publisher="acme-labs")

        second = client.get("/plugins/api/publishers/acme-labs?page=2&limit=1").get_json()

        assert [plugin["id"] for plugin in second["plugins"]] == ["beta"]
        assert second["pagination"] == {
            "page": 2,
            "limit": 1,
            "max_limit": 100,
            "total": 3,
            "pages": 3,
            "has_next": True,
            "has_prev": True,
        }

    def test_empty_publisher_returns_an_empty_page(self, app, client, make_user, login):
        make_user()
        login()

        response = client.get("/plugins/api/publishers/nobody")

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["plugins"] == []
        assert payload["pagination"]["total"] == 0
        assert payload["publisher"]["verified"] is False


class TestPublisherPage:
    def test_page_renders_verified_badge_and_plugins(self, app, client, make_user, login):
        make_user()
        login()
        _trust(app)
        _plugin("alpha", trust_publisher="acme-labs", trust_state="Trusted")

        response = client.get("/plugins/publishers/acme-labs")
        html = response.get_data(as_text=True)

        assert response.status_code == 200
        assert "Verified publisher" in html
        assert "alpha" in html
        assert "Installs" in html

    def test_empty_publisher_page_has_an_empty_state(self, client, make_user, login):
        make_user()
        login()

        response = client.get("/plugins/publishers/nobody")

        assert response.status_code == 200
        assert "has not published any plugins yet" in response.get_data(as_text=True)

    def test_page_requires_authentication(self, client):
        response = client.get("/plugins/publishers/acme-labs")
        assert response.status_code in (302, 401)
