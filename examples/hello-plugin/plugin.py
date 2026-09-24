"""Minimal example plugin for the AI Code Assistant plugin system.

This is the runnable companion to ``examples/hello-plugin/manifest.json`` and
the guide in :doc:`docs/plugins`. It shows the smallest correct plugin:

* a manifest whose ``entry_point`` names this module's :class:`HelloPlugin`;
* the class subscribes to the ``project.created`` event and records every
  event it receives (logging it and appending it to ``received``).

It uses only the public plugin APIs. Subscribing never requires a capability
grant -- the dispatcher checks grants only when the host application actually
dispatches a real event (see ``docs/security.md``).

Run it from the repository root. The repository root must be importable so
``app`` resolves, and this directory must be importable so the manifest's
``entry_point`` (``plugin:HelloPlugin``) resolves::

    # Windows (PowerShell)
    $env:PYTHONPATH = ".;examples/hello-plugin"
    python examples/hello-plugin/plugin.py

    # Linux / macOS
    PYTHONPATH=.:examples/hello-plugin python examples/hello-plugin/plugin.py
"""

from __future__ import annotations

import logging
from pathlib import Path

from app.services.events import Event, get_dispatcher

logger = logging.getLogger("hello_plugin")


class HelloPlugin:
    """A minimal plugin that observes ``project.created`` events."""

    def __init__(self, app=None, manifest=None):
        self.app = app
        self.manifest = manifest
        self.received: list[Event] = []

    @property
    def plugin_id(self) -> str:
        """Manifest id used as the subscription owner."""
        return self.manifest.id if self.manifest is not None else "hello-plugin"

    def subscribe(self) -> None:
        """Subscribe :meth:`on_event` to ``project.created``."""
        get_dispatcher().subscribe("project.created", self.on_event, plugin_id=self.plugin_id)
        logger.info("%s subscribed to project.created", self.plugin_id)

    def unsubscribe(self) -> None:
        """Remove this plugin's ``project.created`` subscription."""
        get_dispatcher().unsubscribe("project.created", self.on_event)

    def on_event(self, event: Event) -> None:
        """Record and log a received event."""
        self.received.append(event)
        logger.info("%s received %s: %s", self.plugin_id, event.event_type, event.data)

    # Optional lifecycle hooks, invoked by PluginRegistry.enable/disable.
    def on_enable(self) -> None:
        self.subscribe()

    def on_disable(self) -> None:
        self.unsubscribe()


def main() -> None:
    """Load the example from its manifest and exercise the registry flow."""
    from app.services.plugins import Plugin, PluginManifest, PluginRegistry

    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")

    manifest = PluginManifest.from_file(Path(__file__).with_name("manifest.json"))
    registry = PluginRegistry()
    plugin = Plugin(manifest)
    plugin.load()  # imports ``plugin:HelloPlugin`` from this example directory
    registry.register(plugin)

    instance: HelloPlugin = plugin.get_instance()
    instance.subscribe()
    try:
        # A host application would instead dispatch real ``project.created``
        # events; here we dispatch one directly so the example is self-contained.
        instance.on_event(
            Event(event_type="project.created", data={"project_id": 42}, workspace_id=1, user_id=1)
        )
    finally:
        instance.unsubscribe()

    print(f"{manifest.id} registered; recorded {len(instance.received)} event(s).")


if __name__ == "__main__":
    main()
