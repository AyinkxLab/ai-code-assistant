"""Marketplace catalog TTL cache (Phase 9, #377)."""

from __future__ import annotations

import json

import pytest

from app.services import marketplace
from app.services.marketplace import (
    cache_ttl,
    load_marketplace_catalog,
    reset_marketplace_cache,
)

HASH = "a" * 64


def _entry(plugin_id: str) -> dict:
    return {
        "id": plugin_id,
        "version": "1.0.0",
        "source": f"https://plugins.example.com/{plugin_id}.zip",
        "integrity": f"sha256:{HASH}",
        "manifest": {
            "id": plugin_id,
            "name": f"Plugin {plugin_id}",
            "version": "1.0.0",
            "description": "A plugin",
            "author": "Acme",
            "entry_point": "plugins.test:TestPlugin",
            "capabilities": ["PROJECT_READ"],
        },
    }


def _write(path, *ids: str) -> None:
    path.write_text(
        json.dumps({"version": 1, "plugins": [_entry(i) for i in ids]}), encoding="utf-8"
    )


@pytest.fixture()
def clock(monkeypatch):
    now = [1000.0]
    monkeypatch.setattr(marketplace.time, "monotonic", lambda: now[0])
    return now


def _config(path, ttl):
    return {
        "PLUGIN_MARKETPLACE_ENABLED": True,
        "PLUGIN_MARKETPLACE_CATALOG": str(path),
        "PLUGIN_MARKETPLACE_CACHE_TTL": ttl,
    }


def _ids(result):
    return [entry.plugin_id for entry in result.catalog.list_entries()]


def test_miss_reads_source_then_hit_avoids_rereading(tmp_path, clock):
    path = tmp_path / "catalog.json"
    _write(path, "alpha")
    config = _config(path, 60)

    assert _ids(load_marketplace_catalog(config)) == ["alpha"]

    # The source changes (and even disappears) but the cache is still fresh.
    _write(path, "alpha", "beta")
    clock[0] += 59
    assert _ids(load_marketplace_catalog(config)) == ["alpha"]
    path.unlink()
    assert _ids(load_marketplace_catalog(config)) == ["alpha"]


def test_entry_expires_after_ttl(tmp_path, clock):
    path = tmp_path / "catalog.json"
    _write(path, "alpha")
    config = _config(path, 60)
    load_marketplace_catalog(config)

    _write(path, "alpha", "beta")
    clock[0] += 60
    assert _ids(load_marketplace_catalog(config)) == ["alpha", "beta"]


def test_cache_is_keyed_by_source(tmp_path, clock):
    first, second = tmp_path / "one.json", tmp_path / "two.json"
    _write(first, "alpha")
    _write(second, "beta")

    assert _ids(load_marketplace_catalog(_config(first, 60))) == ["alpha"]
    assert _ids(load_marketplace_catalog(_config(second, 60))) == ["beta"]


def test_zero_ttl_disables_caching(tmp_path, clock):
    path = tmp_path / "catalog.json"
    _write(path, "alpha")
    config = _config(path, 0)
    load_marketplace_catalog(config)

    _write(path, "alpha", "beta")
    assert _ids(load_marketplace_catalog(config)) == ["alpha", "beta"]


def test_reset_busts_the_cache(tmp_path, clock):
    path = tmp_path / "catalog.json"
    _write(path, "alpha")
    config = _config(path, 60)
    load_marketplace_catalog(config)

    _write(path, "alpha", "beta")
    reset_marketplace_cache()
    assert _ids(load_marketplace_catalog(config)) == ["alpha", "beta"]


def test_unavailable_source_is_not_cached(tmp_path, clock):
    path = tmp_path / "missing.json"
    config = _config(path, 60)
    assert load_marketplace_catalog(config).state == "unavailable"

    _write(path, "alpha")
    assert _ids(load_marketplace_catalog(config)) == ["alpha"]


@pytest.mark.parametrize(
    ("raw", "expected"),
    [(None, 60.0), ("30", 30.0), (0, 0.0), (-5, 0.0), ("bad", 60.0)],
)
def test_cache_ttl_parsing(raw, expected):
    config = {} if raw is None else {"PLUGIN_MARKETPLACE_CACHE_TTL": raw}
    assert cache_ttl(config) == expected
