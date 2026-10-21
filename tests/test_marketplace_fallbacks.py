"""Catalog error handling and last-good fallbacks (#380).

Every failure mode must be explicit and must never surface as a 500: a missing
source falls back to the last good catalog (or an empty one), a malformed
document is a stable 400, and an empty catalog is a normal 200.
"""

from __future__ import annotations

import json

import pytest

from app.services.marketplace import (
    MarketplaceError,
    load_marketplace_catalog,
    reset_marketplace_cache,
)

HASH = "a" * 64


def _manifest(plugin_id: str) -> dict:
    return {
        "id": plugin_id,
        "name": f"Plugin {plugin_id}",
        "version": "1.0.0",
        "description": "A marketplace plugin",
        "author": "Acme",
        "entry_point": "plugins.test:TestPlugin",
        "capabilities": ["PROJECT_READ"],
    }


def _entry(plugin_id: str) -> dict:
    return {
        "id": plugin_id,
        "version": "1.0.0",
        "source": f"https://plugins.example.com/{plugin_id}.zip",
        "integrity": f"sha256:{HASH}",
        "manifest": _manifest(plugin_id),
    }


def _write(tmp_path, plugins, name="catalog.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"version": 1, "plugins": plugins}), encoding="utf-8")
    return path


def _config(path) -> dict:
    return {
        "PLUGIN_MARKETPLACE_ENABLED": True,
        "PLUGIN_MARKETPLACE_CATALOG": str(path),
        "PLUGIN_MARKETPLACE_URL": "",
    }


def _login(app, client, make_user, login):
    make_user(username="member", email="member@example.com")
    app.config["PLUGIN_MARKETPLACE_ENABLED"] = True
    login(email="member@example.com")


@pytest.fixture(autouse=True)
def _clear_last_good_catalogs():
    """Keep the process-local fallback cache from leaking between tests."""
    reset_marketplace_cache()
    yield
    reset_marketplace_cache()


class TestCatalogFailureModes:
    def test_valid_empty_catalog_is_reported_as_empty(self, tmp_path):
        result = load_marketplace_catalog(_config(_write(tmp_path, [])))

        assert len(result.catalog) == 0
        assert result.state == "empty"
        assert result.warnings == ()
        assert result.stale is False
        assert result.unavailable is False

    def test_offline_without_last_good_catalog_is_empty_and_explicit(self, tmp_path):
        result = load_marketplace_catalog(_config(tmp_path / "missing.json"))

        assert len(result.catalog) == 0
        assert result.state == "unavailable"
        assert result.unavailable is True
        assert result.stale is False
        assert any("missing or unreadable" in warning for warning in result.warnings)

    def test_offline_after_a_good_load_serves_the_last_good_catalog(self, tmp_path):
        path = _write(tmp_path, [_entry("alpha")])
        first = load_marketplace_catalog(_config(path))
        assert [entry.plugin_id for entry in first.catalog.list_entries()] == ["alpha"]

        path.unlink()
        fallback = load_marketplace_catalog(_config(path))

        assert [entry.plugin_id for entry in fallback.catalog.list_entries()] == ["alpha"]
        assert fallback.state == "stale"
        assert fallback.stale is True
        assert fallback.unavailable is False
        assert any("last known good" in warning for warning in fallback.warnings)

    def test_non_utf8_catalog_is_invalid_not_unavailable(self, tmp_path):
        path = tmp_path / "catalog.json"
        path.write_bytes(b"\xff\xfe\x00not-utf8")

        with pytest.raises(MarketplaceError, match="UTF-8"):
            load_marketplace_catalog(_config(path))

    def test_invalid_json_has_a_stable_message(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(MarketplaceError, match="invalid catalog JSON"):
            load_marketplace_catalog(_config(path))


class TestCatalogFailureApi:
    def test_offline_api_returns_empty_page_not_500(self, app, client, make_user, login, tmp_path):
        _login(app, client, make_user, login)
        app.config["PLUGIN_MARKETPLACE_CATALOG"] = str(tmp_path / "missing.json")

        response = client.get("/plugins/api/marketplace")

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["entries"] == []
        assert payload["state"] == "unavailable"
        assert payload["warnings"]

    def test_api_serves_last_good_entries_when_the_file_disappears(
        self, app, client, make_user, login, tmp_path
    ):
        _login(app, client, make_user, login)
        path = _write(tmp_path, [_entry("alpha")])
        app.config["PLUGIN_MARKETPLACE_CATALOG"] = str(path)
        assert client.get("/plugins/api/marketplace").status_code == 200

        path.unlink()
        response = client.get("/plugins/api/marketplace")

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["state"] == "stale"
        assert payload["stale"] is True
        assert [entry["id"] for entry in payload["entries"]] == ["alpha"]

    def test_empty_catalog_api_returns_an_empty_state(
        self, app, client, make_user, login, tmp_path
    ):
        _login(app, client, make_user, login)
        app.config["PLUGIN_MARKETPLACE_CATALOG"] = str(_write(tmp_path, []))

        response = client.get("/plugins/api/marketplace")

        assert response.status_code == 200
        assert response.get_json()["state"] == "empty"

    def test_non_utf8_catalog_api_is_a_400_invalid_error(
        self, app, client, make_user, login, tmp_path
    ):
        _login(app, client, make_user, login)
        path = tmp_path / "catalog.json"
        path.write_bytes(b"\xff\xfe\x00not-utf8")
        app.config["PLUGIN_MARKETPLACE_CATALOG"] = str(path)

        response = client.get("/plugins/api/marketplace")

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid"

    def test_invalid_catalog_payload_is_ui_safe(self, app, client, make_user, login, tmp_path):
        _login(app, client, make_user, login)
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")
        app.config["PLUGIN_MARKETPLACE_CATALOG"] = str(path)

        response = client.get("/plugins/api/marketplace")
        payload = response.get_json()

        assert response.status_code == 400
        assert payload == {
            "error": "The plugin catalog is invalid. Check the catalog file and try again.",
            "code": "invalid",
        }
        assert str(tmp_path) not in response.get_data(as_text=True)
