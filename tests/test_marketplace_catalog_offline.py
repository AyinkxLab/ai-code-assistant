"""Comprehensive offline tests for the marketplace catalog service (#381).

Every case uses a local temp file or a mocked transport: no test performs a real
network request. The suite covers valid, invalid, offline, allowlist-refused and
empty catalogs across both the strict loader (:func:`load_marketplace`) and the
lenient discovery loader (:func:`load_marketplace_catalog`).
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services import marketplace as marketplace_service
from app.services.marketplace import (
    MarketplaceError,
    MarketplaceUnavailableError,
    load_marketplace,
    load_marketplace_catalog,
)
from app.services.plugin_catalog import MAX_CATALOG_BYTES, MAX_CATALOG_ENTRIES
from app.services.plugin_network import PluginNetworkError

HASH = "a" * 64
HOST = "plugins.example.com"
PUBLIC_IP = "93.184.216.34"


def _manifest(plugin_id: str, version: str = "0.1.0", **overrides) -> dict:
    manifest = {
        "id": plugin_id,
        "name": f"Plugin {plugin_id}",
        "version": version,
        "description": f"{plugin_id} description",
        "author": "Acme Labs",
        "entry_point": "plugins.test:TestPlugin",
        "capabilities": ["PROJECT_READ"],
        "permissions": ["read:project:files"],
        "dependencies": ["base-plugin>=1.0.0"],
        "compatibility": ">=0.8.0",
    }
    manifest.update(overrides)
    return manifest


def _entry(plugin_id: str, version: str = "0.1.0", **overrides) -> dict:
    entry = {
        "id": plugin_id,
        "version": version,
        "source": f"https://{HOST}/{plugin_id}-{version}.zip",
        "integrity": f"sha256:{HASH}",
        "manifest": _manifest(plugin_id, version),
    }
    entry.update(overrides)
    return entry


def _write_catalog(tmp_path, entries, name="catalog.json"):
    path = tmp_path / name
    path.write_text(json.dumps({"version": 1, "plugins": entries}), encoding="utf-8")
    return path


def _config(path=None, *, url: str = "", allowlist: str = HOST, **overrides) -> dict:
    config = {
        "PLUGIN_MARKETPLACE_ENABLED": True,
        "PLUGIN_MARKETPLACE_CATALOG": "" if path is None else str(path),
        "PLUGIN_MARKETPLACE_URL": url,
        "PLUGIN_NETWORK_ALLOWLIST": allowlist,
        "PLUGIN_NETWORK_HTTPS_ONLY": True,
        "PLUGIN_NETWORK_ALLOW_PRIVATE": False,
        "PLUGIN_NETWORK_TIMEOUT": 5,
        "PLUGIN_NETWORK_MAX_BYTES": 1024 * 1024,
        "PLUGIN_NETWORK_STRICT_DNS": False,
    }
    config.update(overrides)
    return config


def _resolver(host: str) -> list[str]:
    return [PUBLIC_IP]


class _Response:
    """Minimal stand-in for a ``requests`` response (fully offline)."""

    def __init__(self, payload=b"", *, status_code=200, headers=None):
        if not isinstance(payload, (bytes, bytearray)):
            payload = json.dumps(payload).encode("utf-8")
        self.content = bytes(payload)
        self.status_code = status_code
        self.headers = headers or {}


def _transport(response, calls):
    """Return a transport that records calls and returns/raises ``response``."""

    def transport(url, **kwargs):
        calls.append((url, kwargs))
        if isinstance(response, BaseException):
            raise response
        return response

    return transport


class TestValidCatalogs:
    def test_local_catalog_loads_entries_in_deterministic_order(self, tmp_path):
        path = _write_catalog(
            tmp_path,
            [_entry("beta"), _entry("alpha", "0.2.0"), _entry("alpha", "0.10.0")],
        )

        catalog = load_marketplace(_config(path))

        assert [(entry.plugin_id, entry.version) for entry in catalog.list_entries()] == [
            ("alpha", "0.2.0"),
            ("alpha", "0.10.0"),
            ("beta", "0.1.0"),
        ]
        assert catalog.get("alpha").version == "0.10.0"

    def test_local_empty_catalog_is_valid(self, tmp_path):
        path = _write_catalog(tmp_path, [])

        strict = load_marketplace(_config(path))
        lenient = load_marketplace_catalog(_config(path))

        assert len(strict) == 0
        assert len(lenient.catalog) == 0
        assert lenient.warnings == ()

    def test_remote_catalog_uses_the_mocked_transport(self):
        calls = []
        response = _Response({"version": 1, "plugins": [_entry("remote")]})

        catalog = load_marketplace(
            _config(None, url=f"https://{HOST}/catalog.json"),
            resolver=_resolver,
            transport=_transport(response, calls),
        )

        assert [entry.plugin_id for entry in catalog.list_entries()] == ["remote"]
        assert [url for url, _ in calls] == [f"https://{HOST}/catalog.json"]
        assert calls[0][1]["max_bytes"] > 0

    def test_remote_empty_catalog_is_valid(self):
        calls = []
        response = _Response({"version": 1, "plugins": []})

        strict = load_marketplace(
            _config(None, url=f"https://{HOST}/empty.json"),
            resolver=_resolver,
            transport=_transport(response, calls),
        )
        lenient = load_marketplace_catalog(
            _config(None, url=f"https://{HOST}/empty.json"),
            resolver=_resolver,
            transport=_transport(response, calls),
        )

        assert len(strict) == 0
        assert len(lenient.catalog) == 0
        assert lenient.warnings == ()

    def test_signature_metadata_is_preserved_in_the_public_entry(self, tmp_path):
        signature = {"publisher": "acme", "algorithm": "ed25519", "value": "signed"}
        path = _write_catalog(tmp_path, [_entry("signed", signature=signature)])

        catalog = load_marketplace(_config(path))

        assert catalog.get("signed").to_dict()["signature"] == signature


class TestInvalidCatalogs:
    @pytest.mark.parametrize(
        ("raw", "strict_match", "lenient_match"),
        [
            ("{not json", "not valid JSON", "invalid catalog JSON"),
            (json.dumps(["not", "an", "object"]), "must be an object", "JSON object"),
            (json.dumps({"version": 99, "plugins": []}), "unsupported", "unsupported"),
            (json.dumps({"version": 1, "plugins": {}}), "must be a list", "must be a list"),
        ],
    )
    def test_malformed_local_documents_raise(self, tmp_path, raw, strict_match, lenient_match):
        path = tmp_path / "catalog.json"
        path.write_text(raw, encoding="utf-8")

        with pytest.raises(MarketplaceError, match=strict_match):
            load_marketplace(_config(path))

        with pytest.raises(MarketplaceError, match=lenient_match):
            load_marketplace_catalog(_config(path))

    def test_too_many_entries_raise(self, tmp_path):
        path = tmp_path / "large.json"
        document = {"version": 1, "plugins": [{}] * (MAX_CATALOG_ENTRIES + 1)}
        path.write_text(json.dumps(document), encoding="utf-8")

        with pytest.raises(MarketplaceError, match="at most"):
            load_marketplace(_config(path))

        with pytest.raises(MarketplaceError, match="at most"):
            load_marketplace_catalog(_config(path))

    @pytest.mark.parametrize(
        ("entry", "match"),
        [
            (_entry("Alpha"), "lowercase"),
            (_entry("alpha", "not-a-version"), "valid version"),
            (_entry("alpha", source="http://plugins.example.com/alpha.zip"), "https"),
            (_entry("alpha", integrity="not-a-hash"), "integrity"),
            (_entry("alpha", manifest="not-an-object"), "must be an object"),
            (_entry("alpha", manifest=_manifest("other")), "does not match"),
            (_entry("alpha", manifest=_manifest("alpha", "9.9.9")), "does not match"),
        ],
    )
    def test_invalid_entries_raise(self, tmp_path, entry, match):
        path = _write_catalog(tmp_path, [entry])

        with pytest.raises(MarketplaceError, match=match):
            load_marketplace(_config(path))

    def test_duplicate_entries_raise(self, tmp_path):
        path = _write_catalog(tmp_path, [_entry("alpha"), _entry("alpha")])

        with pytest.raises(MarketplaceError, match="duplicate"):
            load_marketplace(_config(path))

    def test_unexpected_manifest_schema_shape_is_invalid(self, tmp_path, monkeypatch):
        schema = tmp_path / "plugin.schema.json"
        schema.write_text("[]", encoding="utf-8")
        path = _write_catalog(tmp_path, [_entry("alpha")])
        monkeypatch.setattr(marketplace_service, "MANIFEST_SCHEMA_PATH", schema)
        marketplace_service._load_manifest_schema.cache_clear()
        try:
            with pytest.raises(MarketplaceError, match="JSON object"):
                load_marketplace(_config(path))
        finally:
            marketplace_service._load_manifest_schema.cache_clear()

    @pytest.mark.parametrize(
        ("response", "match"),
        [
            (_Response(b"{not json"), "not valid JSON"),
            (_Response(b"\xff\xfe\x00"), "UTF-8"),
            (_Response({"version": 99, "plugins": []}), "unsupported"),
        ],
    )
    def test_invalid_remote_documents_raise(self, response, match):
        calls = []

        with pytest.raises(MarketplaceError, match=match):
            load_marketplace(
                _config(None, url=f"https://{HOST}/catalog.json"),
                resolver=_resolver,
                transport=_transport(response, calls),
            )

        with pytest.raises(MarketplaceError, match=match):
            load_marketplace_catalog(
                _config(None, url=f"https://{HOST}/catalog.json"),
                resolver=_resolver,
                transport=_transport(response, calls),
            )

        assert calls

    def test_lenient_loader_skips_invalid_entries_but_keeps_valid_ones(self, tmp_path):
        bad = _entry("bad")
        bad["manifest"].pop("entry_point")
        path = _write_catalog(tmp_path, [_entry("good"), bad])

        result = load_marketplace_catalog(_config(path))

        assert [entry.plugin_id for entry in result.catalog.list_entries()] == ["good"]
        assert len(result.warnings) == 1
        assert "plugins[1]" in result.warnings[0]


class TestOfflineCatalogs:
    def test_missing_local_file(self, tmp_path):
        path = tmp_path / "missing.json"

        with pytest.raises(MarketplaceUnavailableError):
            load_marketplace(_config(path))

        result = load_marketplace_catalog(_config(path))
        assert len(result.catalog) == 0
        assert any("missing or unreadable" in warning for warning in result.warnings)

    def test_directory_path_is_unavailable(self, tmp_path):
        directory = tmp_path / "catalog-dir"
        directory.mkdir()

        with pytest.raises(MarketplaceUnavailableError):
            load_marketplace(_config(directory))

        result = load_marketplace_catalog(_config(directory))
        assert len(result.catalog) == 0
        assert result.warnings

    def test_read_error_after_stat_is_treated_as_offline(self, tmp_path, monkeypatch):
        path = _write_catalog(tmp_path, [_entry("alpha")])

        def unreadable(self, *args, **kwargs):
            raise OSError("permission denied")

        monkeypatch.setattr(Path, "read_text", unreadable)

        result = load_marketplace_catalog(_config(path))

        assert len(result.catalog) == 0
        assert any("missing or unreadable" in warning for warning in result.warnings)

    def test_oversized_local_file_is_unavailable(self, tmp_path):
        path = tmp_path / "huge.json"
        path.write_bytes(b"{" + b" " * MAX_CATALOG_BYTES + b"}")

        with pytest.raises(MarketplaceUnavailableError, match="exceeds"):
            load_marketplace(_config(path))

        result = load_marketplace_catalog(_config(path))
        assert len(result.catalog) == 0
        assert any("missing or unreadable" in warning for warning in result.warnings)

    def test_non_https_remote_url_is_refused_before_transport(self):
        calls = []

        with pytest.raises(MarketplaceUnavailableError, match="https"):
            load_marketplace(
                _config(None, url=f"http://{HOST}/catalog.json"),
                resolver=_resolver,
                transport=_transport(_Response({"version": 1, "plugins": []}), calls),
            )

        assert calls == []

    def test_non_allowlisted_host_is_refused_before_transport(self):
        calls = []

        with pytest.raises(MarketplaceUnavailableError, match="allowlist"):
            load_marketplace(
                _config(None, url="https://evil.example.com/catalog.json"),
                resolver=_resolver,
                transport=_transport(_Response({"version": 1, "plugins": []}), calls),
            )

        assert calls == []

    def test_private_resolution_is_refused_before_transport(self):
        calls = []

        with pytest.raises(MarketplaceUnavailableError, match="private"):
            load_marketplace(
                _config(None, url=f"https://{HOST}/catalog.json"),
                resolver=lambda host: ["127.0.0.1"],
                transport=_transport(_Response({"version": 1, "plugins": []}), calls),
            )

        assert calls == []

    @pytest.mark.parametrize(
        "failure",
        [
            PluginNetworkError("connection refused"),
            RuntimeError("socket closed"),
        ],
    )
    def test_transport_failures_are_unavailable(self, failure):
        calls = []

        with pytest.raises(MarketplaceUnavailableError):
            load_marketplace(
                _config(None, url=f"https://{HOST}/catalog.json"),
                resolver=_resolver,
                transport=_transport(failure, calls),
            )

        result = load_marketplace_catalog(
            _config(None, url=f"https://{HOST}/catalog.json"),
            resolver=_resolver,
            transport=_transport(failure, calls),
        )
        assert len(result.catalog) == 0
        assert result.warnings

    @pytest.mark.parametrize(
        "response",
        [
            _Response({"version": 1, "plugins": []}, status_code=500),
            _Response({"version": 1, "plugins": []}, status_code=302),
        ],
    )
    def test_unexpected_and_redirect_statuses_are_unavailable(self, response):
        calls = []

        with pytest.raises(MarketplaceUnavailableError):
            load_marketplace(
                _config(None, url=f"https://{HOST}/catalog.json"),
                resolver=_resolver,
                transport=_transport(response, calls),
            )

        result = load_marketplace_catalog(
            _config(None, url=f"https://{HOST}/catalog.json"),
            resolver=_resolver,
            transport=_transport(response, calls),
        )
        assert len(result.catalog) == 0
        assert result.warnings

    def test_declared_and_actual_oversized_bodies_are_unavailable(self):
        too_large = str(MAX_CATALOG_BYTES + 1)
        declared = _Response(
            {"version": 1, "plugins": []},
            headers={"Content-Length": too_large},
        )
        actual = _Response(b" " * (MAX_CATALOG_BYTES + 1))

        for response in (declared, actual):
            with pytest.raises(MarketplaceUnavailableError, match="exceeds"):
                load_marketplace(
                    _config(None, url=f"https://{HOST}/catalog.json"),
                    resolver=_resolver,
                    transport=_transport(response, []),
                )


class TestCatalogBranchCoverage:
    def test_unknown_entry_raises(self, tmp_path):
        catalog = load_marketplace(_config(_write_catalog(tmp_path, [_entry("alpha")])))

        with pytest.raises(MarketplaceError, match="not in the marketplace"):
            catalog.get("missing")

        with pytest.raises(MarketplaceError, match="version"):
            catalog.get("alpha", "9.9.9")

    def test_disabled_marketplace_raises_before_reading_the_catalog(self, tmp_path):
        with pytest.raises(MarketplaceError, match="disabled"):
            load_marketplace(_config(tmp_path / "missing.json", PLUGIN_MARKETPLACE_ENABLED=False))

        with pytest.raises(MarketplaceError, match="disabled"):
            load_marketplace_catalog(
                _config(tmp_path / "missing.json", PLUGIN_MARKETPLACE_ENABLED=False)
            )

    def test_default_catalog_path_is_used_when_nothing_is_configured(self):
        catalog = load_marketplace(
            {"PLUGIN_MARKETPLACE_ENABLED": True, "PLUGIN_MARKETPLACE_CATALOG": ""}
        )

        assert len(catalog) == 0
