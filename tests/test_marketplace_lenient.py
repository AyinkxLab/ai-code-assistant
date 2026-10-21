"""Tests for the lenient local marketplace loader (#374/#376)."""

from __future__ import annotations

import json

import pytest

from app.services.marketplace import (
    MAX_MARKETPLACE_WARNINGS,
    MarketplaceError,
    load_marketplace_catalog,
)

HASH = "a" * 64


def _manifest(plugin_id: str, **overrides):
    manifest = {
        "id": plugin_id,
        "name": f"Plugin {plugin_id}",
        "version": "1.0.0",
        "description": "A plugin",
        "author": "Acme",
        "entry_point": "plugins.test:TestPlugin",
        "capabilities": ["PROJECT_READ"],
    }
    manifest.update(overrides)
    return manifest


def _entry(plugin_id: str, **overrides):
    entry = {
        "id": plugin_id,
        "version": "1.0.0",
        "source": f"https://plugins.example.com/{plugin_id}.zip",
        "integrity": f"sha256:{HASH}",
        "manifest": _manifest(plugin_id),
    }
    entry.update(overrides)
    return entry


def _config(path, **overrides):
    config = {
        "PLUGIN_MARKETPLACE_ENABLED": True,
        "PLUGIN_MARKETPLACE_CATALOG": str(path),
        "PLUGIN_MARKETPLACE_URL": "",
    }
    config.update(overrides)
    return config


def _write(tmp_path, entries, name="catalog.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"version": 1, "plugins": entries}), encoding="utf-8")
    return path


class TestLenientLoader:
    def test_valid_catalog_parses(self, tmp_path):
        path = _write(tmp_path, [_entry("alpha")])

        result = load_marketplace_catalog(_config(path))

        assert [entry.plugin_id for entry in result.catalog.list_entries()] == ["alpha"]
        assert result.warnings == ()

    def test_missing_file_yields_empty_catalog_without_error(self, tmp_path):
        result = load_marketplace_catalog(_config(tmp_path / "missing.json"))

        assert len(result.catalog) == 0
        assert len(result.warnings) == 1
        assert "missing" in result.warnings[0]

    def test_unreadable_path_yields_empty_catalog(self, tmp_path):
        directory = tmp_path / "not-a-file"
        directory.mkdir()

        result = load_marketplace_catalog(_config(directory))

        assert len(result.catalog) == 0
        assert result.warnings

    def test_invalid_entries_are_dropped_with_warnings(self, tmp_path):
        invalid_manifest = _entry("no-entry-point")
        invalid_manifest["manifest"].pop("entry_point")
        invalid_id = _entry("Bad Id")
        invalid_integrity = _entry("bad-integrity", integrity="not-a-hash")
        path = _write(
            tmp_path,
            [_entry("alpha"), invalid_manifest, invalid_id, invalid_integrity],
        )

        result = load_marketplace_catalog(_config(path))

        assert [entry.plugin_id for entry in result.catalog.list_entries()] == ["alpha"]
        assert len(result.warnings) == 3
        assert any("entry_point" in warning for warning in result.warnings)
        assert any("plugins[2]" in warning for warning in result.warnings)

    def test_warnings_are_bounded(self, tmp_path):
        entries = []
        for index in range(MAX_MARKETPLACE_WARNINGS + 5):
            entry = _entry(f"bad-{index}")
            entry["manifest"].pop("entry_point")
            entries.append(entry)
        path = _write(tmp_path, entries)

        result = load_marketplace_catalog(_config(path))

        assert len(result.catalog) == 0
        assert len(result.warnings) == MAX_MARKETPLACE_WARNINGS

    def test_malformed_json_still_raises(self, tmp_path):
        path = tmp_path / "broken.json"
        path.write_text("{not json", encoding="utf-8")

        with pytest.raises(MarketplaceError, match="invalid catalog JSON"):
            load_marketplace_catalog(_config(path))

    def test_wrong_document_shape_raises(self, tmp_path):
        path = tmp_path / "shape.json"
        path.write_text(json.dumps({"version": 1, "plugins": {}}), encoding="utf-8")

        with pytest.raises(MarketplaceError, match="must be a list"):
            load_marketplace_catalog(_config(path))


class TestLenientApi:
    def _login(self, app, client, make_user, login):
        make_user(username="member", email="member@example.com")
        app.config["PLUGIN_MARKETPLACE_ENABLED"] = True
        login(email="member@example.com")

    def test_missing_catalog_returns_empty_page(self, app, client, make_user, login, tmp_path):
        self._login(app, client, make_user, login)
        app.config["PLUGIN_MARKETPLACE_CATALOG"] = str(tmp_path / "missing.json")

        response = client.get("/plugins/api/marketplace")

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["entries"] == []
        assert payload["pagination"]["total"] == 0
        assert payload["warnings"]

    def test_invalid_entries_are_not_advertised(self, app, client, make_user, login, tmp_path):
        self._login(app, client, make_user, login)
        bad = _entry("bad")
        bad["manifest"].pop("entry_point")
        app.config["PLUGIN_MARKETPLACE_CATALOG"] = str(_write(tmp_path, [_entry("alpha"), bad]))

        response = client.get("/plugins/api/marketplace")

        assert response.status_code == 200
        payload = response.get_json()
        assert [entry["id"] for entry in payload["entries"]] == ["alpha"]
        assert len(payload["warnings"]) == 1

    def test_malformed_catalog_is_a_client_error(self, app, client, make_user, login, tmp_path):
        self._login(app, client, make_user, login)
        broken = tmp_path / "broken.json"
        broken.write_text("{", encoding="utf-8")
        app.config["PLUGIN_MARKETPLACE_CATALOG"] = str(broken)

        response = client.get("/plugins/api/marketplace")

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid"
