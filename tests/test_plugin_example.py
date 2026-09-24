"""Validates the example plugin used by docs/plugin-developer-guide.md (#198).

Keeps the guide's end-to-end example honest: the manifest must pass the real
validator and the plugin class must implement the documented contract.
"""

import importlib
import sys
from pathlib import Path
from types import SimpleNamespace

EXAMPLES_DIR = Path(__file__).resolve().parent.parent / "examples" / "plugins"
PLUGIN_DIR = EXAMPLES_DIR / "hello_world"


def _load_example_module(monkeypatch):
    monkeypatch.syspath_prepend(str(EXAMPLES_DIR))
    sys.modules.pop("hello_world.plugin", None)
    return importlib.import_module("hello_world.plugin")


def test_manifest_is_valid():
    from app.services.plugins import PluginManifest

    manifest = PluginManifest.from_file(PLUGIN_DIR / "manifest.json")
    assert manifest.id == "hello-world"
    assert manifest.name == "Hello World"
    assert manifest.version == "0.1.0"
    assert manifest.entry_point == "hello_world.plugin:HelloWorldPlugin"
    assert manifest.capabilities == ["PROJECT_READ"]


def test_plugin_class_contract(monkeypatch):
    module = _load_example_module(monkeypatch)
    plugin = module.HelloWorldPlugin(app=None, manifest=None)

    assert callable(plugin.register)
    assert callable(plugin.on_enable)
    assert callable(plugin.on_disable)

    event = SimpleNamespace(event_type="project.created", data={"project_id": 7})
    assert plugin.on_event(event) == {
        "plugin": "hello-world",
        "event": "project.created",
        "project_id": 7,
    }
    assert plugin.seen_project_ids == [7]


def test_plugin_registers_with_its_plugin_id(monkeypatch):
    module = _load_example_module(monkeypatch)
    plugin = module.HelloWorldPlugin()

    from app.services.events import get_dispatcher

    dispatcher = get_dispatcher()
    plugin.register()
    try:
        subscribers = dispatcher._subscribers.get("project.created", [])
        assert ("hello-world", plugin.on_event) in subscribers
    finally:
        plugin.on_disable()
