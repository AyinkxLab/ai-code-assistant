"""Tests for the ``examples/hello-plugin`` example (#170).

The example must stay valid against the published manifest schema and must be
loadable/registerable through the real plugin services, so it keeps working as
the plugin system evolves.
"""

from __future__ import annotations

import importlib
import json
import re
import sys
from pathlib import Path

import pytest

from app.services.events import Event, get_dispatcher
from app.services.plugins import Plugin, PluginManifest, PluginRegistry

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_DIR = REPO_ROOT / "examples" / "hello-plugin"
MANIFEST_PATH = EXAMPLE_DIR / "manifest.json"
SCHEMA_PATH = REPO_ROOT / "plugins" / "plugin.schema.json"


def _load_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


@pytest.fixture()
def example_on_path():
    """Make the hyphenated example directory importable as ``plugin``."""
    sys.path.insert(0, str(EXAMPLE_DIR))
    try:
        yield
    finally:
        sys.path.remove(str(EXAMPLE_DIR))
        sys.modules.pop("plugin", None)


class TestManifestAgainstSchema:
    """The example manifest must satisfy the published JSON schema."""

    def test_manifest_matches_published_schema(self):
        manifest = _load_json(MANIFEST_PATH)
        schema = _load_json(SCHEMA_PATH)
        properties = schema["properties"]

        # Required fields are present and there are no unexpected fields
        # (the schema sets additionalProperties: false).
        assert set(schema["required"]) <= set(manifest)
        assert schema["additionalProperties"] is False
        assert set(manifest) <= set(properties)

        assert re.match(properties["id"]["pattern"], manifest["id"])
        assert re.match(properties["version"]["pattern"], manifest["version"])
        assert re.match(properties["entry_point"]["pattern"], manifest["entry_point"])

        allowed_capabilities = set(properties["capabilities"]["items"]["enum"])
        assert manifest["capabilities"]
        assert set(manifest["capabilities"]) <= allowed_capabilities

    def test_manifest_parses_with_app_validator(self):
        manifest = PluginManifest.from_file(MANIFEST_PATH)

        assert manifest.id == "hello-plugin"
        assert manifest.entry_point == "plugin:HelloPlugin"
        assert manifest.capabilities == ["PROJECT_READ"]


class TestLoadAndRegister:
    """The example must load and register through the real plugin services."""

    def test_plugin_loads_and_registers(self, example_on_path):
        manifest = PluginManifest.from_file(MANIFEST_PATH)
        registry = PluginRegistry()
        plugin = Plugin(manifest)

        plugin.load()
        registry.register(plugin)

        assert registry.get("hello-plugin") is plugin
        assert plugin.module is not None
        assert getattr(plugin.module, "HelloPlugin", None) is not None

    def test_plugin_subscribes_and_records_events(self, example_on_path):
        manifest = PluginManifest.from_file(MANIFEST_PATH)
        plugin = Plugin(manifest)
        plugin.load()
        instance = plugin.get_instance()

        instance.subscribe()
        try:
            subscribers = get_dispatcher().list_subscribers("project.created")
            assert "hello-plugin" in subscribers["project.created"]

            event = Event(event_type="project.created", data={"project_id": 7})
            instance.on_event(event)
        finally:
            instance.unsubscribe()

        assert [e.data["project_id"] for e in instance.received] == [7]

    def test_main_runs_end_to_end(self, example_on_path, capsys):
        module = importlib.import_module("plugin")

        module.main()

        output = capsys.readouterr().out
        assert "hello-plugin registered; recorded 1 event(s)." in output
