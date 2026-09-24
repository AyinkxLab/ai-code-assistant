"""Hello World example plugin.

See ``docs/plugin-developer-guide.md`` for the full walkthrough. Importing this
module has no side effects: nothing is subscribed until ``register()`` (called
from ``on_enable``) runs.
"""

from __future__ import annotations

from app.services.events import get_dispatcher

PLUGIN_ID = "hello-world"
EVENT_TYPE = "project.created"


class HelloWorldPlugin:
    """Record ``project.created`` events and expose the latest one."""

    def __init__(self, app=None, manifest=None):
        self.app = app
        self.manifest = manifest
        self.seen_project_ids: list[int] = []

    def register(self) -> None:
        """Subscribe to ``project.created`` (idempotent)."""
        dispatcher = get_dispatcher()
        dispatcher.unsubscribe(EVENT_TYPE, self.on_event)
        dispatcher.subscribe(EVENT_TYPE, self.on_event, plugin_id=PLUGIN_ID)

    def on_event(self, event):
        """Handle an event. Handlers must not raise; return safe data instead."""
        project_id = (event.data or {}).get("project_id")
        self.seen_project_ids.append(project_id)
        return {"plugin": PLUGIN_ID, "event": event.event_type, "project_id": project_id}

    def on_enable(self) -> None:
        """Lifecycle hook: subscribe once the plugin is enabled."""
        self.register()

    def on_disable(self) -> None:
        """Lifecycle hook: stop receiving events while disabled."""
        get_dispatcher().unsubscribe(EVENT_TYPE, self.on_event)
