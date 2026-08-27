"""Event system for plugins and application integration.

Provides centralized event dispatch with plugin subscription support.

Authorization model
-------------------
Before a subscribed plugin handler runs, the dispatcher verifies (during
``dispatch``):

- the event type is supported and has a required capability mapped;
- the plugin exists (``Plugin`` row) and is enabled;
- for workspace-scoped events the event carries a valid ``workspace_id``, the
  plugin is installed (``PluginInstallation``) and enabled in that workspace,
  the emitting user is authorized for the workspace, and the plugin holds the
  capability required by ``EVENT_CAPABILITY_MAP`` through an explicit
  ``CapabilityGrant`` for that workspace.

Subscribers registered *without* a plugin id are internal handlers (trusted
application code) and are not subject to plugin capability enforcement.

Authorization is fail-closed: when any required condition cannot be proven,
the handler is skipped (recorded as denied) and is never invoked. Handler
exceptions remain isolated - one failing handler never prevents other
handlers from running or crashes the request.
"""

import logging
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from app.extensions import db
from app.models import Plugin, PluginInstallation, Project, User, Workspace
from app.services.capabilities import Capability, CapabilityStore
from app.services.permissions import role_for

logger = logging.getLogger(__name__)


# Predefined event types that plugins can subscribe to
SUPPORTED_EVENTS = {
    # Project events
    "project.created": "Project imported or created",
    "project.updated": "Project metadata/config changed",
    "project.deleted": "Project deleted",
    # Review events
    "review.created": "Code review started",
    "review.completed": "Code review finished",
    "review.finding.added": "Review finding added",
    # Workspace events
    "workspace.created": "Workspace created",
    "workspace.member_added": "Member added to workspace",
    "workspace.member_removed": "Member removed from workspace",
    # GitHub events
    "github.connected": "GitHub account connected",
    "github.disconnected": "GitHub account disconnected",
    # AI events
    "ai.analysis.completed": "AI analysis complete",
    # Stellar events
    "stellar.analysis.completed": "Stellar project analysis complete",
    "stellar.network.detected": "Stellar network detected in project",
}

#: Capability a plugin must hold to receive each event type. Capabilities are
#: enforced against explicit per-workspace ``CapabilityGrant`` rows at dispatch
#: time; the manifest's declared capabilities are never used as authorization.
EVENT_CAPABILITY_MAP: dict[str, Capability] = {
    # Project events
    "project.created": Capability.PROJECT_READ,
    "project.updated": Capability.PROJECT_READ,
    "project.deleted": Capability.PROJECT_READ,
    # Review events
    "review.created": Capability.REVIEW_READ,
    "review.completed": Capability.REVIEW_READ,
    "review.finding.added": Capability.REVIEW_READ,
    # Workspace events
    "workspace.created": Capability.WORKSPACE_READ,
    "workspace.member_added": Capability.WORKSPACE_READ,
    "workspace.member_removed": Capability.WORKSPACE_READ,
    # GitHub events
    "github.connected": Capability.GITHUB_READ,
    "github.disconnected": Capability.GITHUB_READ,
    # AI events
    "ai.analysis.completed": Capability.AI_ACCESS,
    # Stellar events
    "stellar.analysis.completed": Capability.STELLAR_READ,
    "stellar.network.detected": Capability.STELLAR_READ,
}

#: Events with no workspace context (user/global scope). Capability grants are
#: workspace-scoped, so these events are delivered to enabled plugins without
#: a per-workspace grant check. Any event not listed here is workspace-scoped.
GLOBAL_EVENTS: frozenset[str] = frozenset({"github.connected", "github.disconnected"})

#: Events whose payload references a ``project_id`` that must belong to the
#: event's workspace (confused-deputy defense). ``project.deleted`` is emitted
#: after the project row is removed, so its project/workspace binding cannot be
#: re-verified and is trusted to come from the owner-scoped route.
PROJECT_CONTEXT_EVENTS: frozenset[str] = frozenset(
    {
        "project.created",
        "project.updated",
        "project.deleted",
        "ai.analysis.completed",
        "stellar.analysis.completed",
        "stellar.network.detected",
    }
)

#: Project-context events where the project row still exists at dispatch time.
_PROJECT_VERIFIABLE_EVENTS: frozenset[str] = PROJECT_CONTEXT_EVENTS - {"project.deleted"}


@dataclass
class Event:
    """Represents an event in the system.

    Events carry type information, data payload, and optional context
    about where they occurred (workspace, user).
    """

    event_type: str
    data: dict[str, Any] | None = None
    timestamp: datetime | None = None
    workspace_id: int | None = None
    user_id: int | None = None

    def __post_init__(self) -> None:
        """Normalize defaults."""
        if self.data is None:
            self.data = {}
        if self.timestamp is None:
            self.timestamp = datetime.now(UTC)

    def to_dict(self) -> dict[str, Any]:
        """Convert event to dictionary for serialization."""
        return {
            "event_type": self.event_type,
            "data": self.data or {},
            "timestamp": self.timestamp.isoformat() if self.timestamp else None,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
        }

    def __repr__(self) -> str:
        """String representation."""
        return f"<Event {self.event_type} at {self.timestamp}>"


class EventError(Exception):
    """Base exception for event system."""

    pass


class EventDispatcher:
    """Central event dispatch and subscription management.

    The dispatcher maintains a registry of event handlers and dispatches
    events to all subscribers. Handler failures are isolated to prevent
    cascade effects.
    """

    def __init__(self) -> None:
        """Initialize dispatcher."""
        # event_type -> list of (plugin_id, handler_func) tuples
        self._subscribers: dict[str, list[tuple[str, Callable]]] = {}

    def subscribe(
        self,
        event_type: str,
        handler: Callable,
        plugin_id: str | None = None,
    ) -> None:
        """Subscribe to an event type.

        Args:
            event_type: Event type to subscribe to (must be in SUPPORTED_EVENTS)
            handler: Callable to invoke when event is dispatched
            plugin_id: Plugin identifier. When ``None`` the subscriber is
                treated as an internal handler (trusted application code) and
                is exempt from plugin capability enforcement. When a plugin id
                is supplied the handler is only invoked if the plugin is
                authorized for the event (see class docstring).

        Raises:
            EventError: If event type is not supported
        """
        if event_type not in SUPPORTED_EVENTS:
            raise EventError(f"Unsupported event type: {event_type}")

        if event_type not in self._subscribers:
            self._subscribers[event_type] = []

        self._subscribers[event_type].append((plugin_id, handler))
        logger.debug(f"Subscribed {plugin_id or 'handler'} to {event_type}")

    def unsubscribe(
        self,
        event_type: str,
        handler: Callable,
    ) -> bool:
        """Unsubscribe from an event type.

        Args:
            event_type: Event type to unsubscribe from
            handler: Handler function to remove

        Returns:
            True if handler was found and removed, False otherwise
        """
        if event_type not in self._subscribers:
            return False

        original_count = len(self._subscribers[event_type])
        self._subscribers[event_type] = [
            (plugin_id, h) for plugin_id, h in self._subscribers[event_type] if h != handler
        ]
        return len(self._subscribers[event_type]) < original_count

    def dispatch(
        self,
        event: Event,
        raise_on_error: bool = False,
    ) -> dict[str, Any]:
        """Dispatch an event to all subscribed handlers.

        Plugin subscribers are authorized before their handler is invoked
        (fail closed - see the module docstring). Internal handlers (subscribed
        without a plugin id) are delivered without capability checks. Handler
        failures are isolated: if one handler fails, others still run.

        Args:
            event: Event to dispatch
            raise_on_error: If True, re-raise first handler error

        Returns:
            Dictionary with dispatch results:
            {
                "event_type": str,
                "total_handlers": int,
                "successful": int,
                "failed": int,
                "denied": int,
                "errors": list of (plugin_id, exception) tuples,
                "denials": list of (plugin_id, reason) tuples
            }
        """
        if event.event_type not in SUPPORTED_EVENTS:
            return {
                "event_type": event.event_type,
                "total_handlers": 0,
                "successful": 0,
                "failed": 0,
                "denied": 0,
                "errors": [],
                "denials": [],
            }

        handlers = self._subscribers.get(event.event_type, [])
        results = {
            "event_type": event.event_type,
            "total_handlers": len(handlers),
            "successful": 0,
            "failed": 0,
            "denied": 0,
            "errors": [],
            "denials": [],
        }

        for plugin_id, handler in handlers:
            if plugin_id is not None:
                try:
                    allowed, reason = self._authorize_plugin(plugin_id, event)
                except Exception as exc:  # never let a lookup failure run a handler
                    allowed, reason = False, "authorization check failed"
                    logger.error(
                        "Authorization error for event %s to plugin %s: %s",
                        event.event_type,
                        plugin_id,
                        exc,
                        exc_info=True,
                    )
                if not allowed:
                    results["denied"] += 1
                    results["denials"].append((plugin_id, reason))
                    logger.info(
                        "Denied event %s to plugin %s: %s",
                        event.event_type,
                        plugin_id,
                        reason,
                    )
                    continue
            try:
                handler(event=event)
                results["successful"] += 1
            except Exception as e:
                results["failed"] += 1
                results["errors"].append((plugin_id or "unknown", e))
                logger.error(
                    f"Error dispatching {event.event_type} to plugin {plugin_id or 'unknown'}: {e}",
                    exc_info=True,
                )
                if raise_on_error:
                    raise

        return results

    def _authorize_plugin(self, plugin_id: str, event: Event) -> tuple[bool, str]:
        """Return ``(allowed, reason)`` for a plugin subscriber.

        Fail closed: every condition must be provable or the handler is denied.
        """
        if event.event_type not in SUPPORTED_EVENTS:
            return False, "unsupported event"

        plugin = db.session.get(Plugin, plugin_id)
        if plugin is None:
            return False, "unknown plugin"
        if not plugin.enabled:
            return False, "plugin disabled"

        capability = EVENT_CAPABILITY_MAP.get(event.event_type)
        if capability is None:
            return False, "event has no mapped capability"

        if event.event_type in GLOBAL_EVENTS:
            return True, ""

        # Workspace-scoped authorization below. A grant is only ever checked
        # against the event's own workspace, never another one.
        if event.workspace_id is None:
            return False, "missing workspace context"

        workspace = db.session.get(Workspace, event.workspace_id)
        if workspace is None:
            return False, "unknown workspace"

        installation = PluginInstallation.query.filter_by(
            plugin_id=plugin_id,
            workspace_id=event.workspace_id,
        ).first()
        if installation is None:
            return False, "plugin not installed in workspace"
        if not installation.enabled:
            return False, "plugin installation disabled"

        if not self._emitting_user_authorized(event):
            return False, "user not authorized for workspace"

        if not self._project_context_valid(event):
            return False, "project/workspace mismatch"

        if not CapabilityStore.has_capability(plugin_id, event.workspace_id, capability.value):
            return False, "missing capability grant"

        return True, ""

    def _emitting_user_authorized(self, event: Event) -> bool:
        """Return ``True`` when the emitting user is authorized for the workspace."""
        if event.user_id is None:
            return False
        user = db.session.get(User, event.user_id)
        if user is None:
            return False
        return role_for(event.workspace_id, user) is not None

    def _project_context_valid(self, event: Event) -> bool:
        """Return ``True`` when a project referenced by the event belongs to the workspace.

        Prevents confused-deputy delivery where an event for one project is
        authorized using another project's (or workspace's) context.
        """
        if event.event_type not in _PROJECT_VERIFIABLE_EVENTS:
            return True
        project_id = (event.data or {}).get("project_id")
        if project_id is None:
            return False
        project = db.session.get(Project, project_id)
        if project is None:
            return False
        return project.workspace_id == event.workspace_id

    def dispatch_async(self, event: Event) -> None:
        """Dispatch event asynchronously (placeholder for job queue).

        Currently dispatches synchronously. In future, could use Celery,
        RQ, or similar job queue for true async dispatch.

        Args:
            event: Event to dispatch
        """
        # TODO: Implement with job queue (Celery, RQ, etc.)
        self.dispatch(event)

    def list_subscribers(self, event_type: str | None = None) -> dict[str, list[str]]:
        """List all subscribers.

        Args:
            event_type: Filter by event type (optional)

        Returns:
            Dictionary mapping event types to list of plugin IDs (internal
            handlers are shown as "unknown")
        """
        if event_type:
            if event_type not in self._subscribers:
                return {event_type: []}
            plugin_ids = [plugin_id or "unknown" for plugin_id, _ in self._subscribers[event_type]]
            return {event_type: plugin_ids}

        result = {}
        for event_t, handlers in self._subscribers.items():
            result[event_t] = [plugin_id or "unknown" for plugin_id, _ in handlers]
        return result

    def clear(self) -> None:
        """Remove all subscriptions.

        Intended for application shutdown and for resetting the shared
        dispatcher between tests.
        """
        self._subscribers.clear()

    @staticmethod
    def list_supported_events() -> dict[str, str]:
        """List all supported event types with descriptions.

        Returns:
            Dictionary mapping event type to description
        """
        return SUPPORTED_EVENTS.copy()

    def get_subscription_count(self) -> int:
        """Get total number of subscriptions.

        Returns:
            Total count of all subscriptions
        """
        return sum(len(handlers) for handlers in self._subscribers.values())

    def __repr__(self) -> str:
        """String representation."""
        return (
            f"<EventDispatcher with {len(self._subscribers)} event types, "
            f"{self.get_subscription_count()} subscriptions>"
        )


# Global dispatcher instance (singleton pattern)
# In production Flask app, this would be bound to app context
_dispatcher: EventDispatcher | None = None


def get_dispatcher() -> EventDispatcher:
    """Get or create the global event dispatcher."""
    global _dispatcher
    if _dispatcher is None:
        _dispatcher = EventDispatcher()
    return _dispatcher


def create_event(
    event_type: str,
    data: dict[str, Any] | None = None,
    workspace_id: int | None = None,
    user_id: int | None = None,
) -> Event:
    """Factory function to create and validate an event.

    Args:
        event_type: Type of event
        data: Event payload data
        workspace_id: Workspace context (if applicable)
        user_id: User context (if applicable)

    Returns:
        Event instance

    Raises:
        EventError: If event type is not supported
    """
    if event_type not in SUPPORTED_EVENTS:
        raise EventError(f"Unsupported event type: {event_type}")

    return Event(
        event_type=event_type,
        data=data or {},
        workspace_id=workspace_id,
        user_id=user_id,
    )


def emit_event(
    event_type: str,
    data: dict[str, Any] | None = None,
    workspace_id: int | None = None,
    user_id: int | None = None,
) -> dict[str, Any]:
    """Dispatch a supported event without ever raising.

    ``emit_event`` is the route-facing wrapper: it validates the event type,
    dispatches to the global dispatcher, and swallows+logs any failure so that
    plugin handler errors can never crash a request. Returns the dispatch
    result dict (empty on failure).
    """
    try:
        event = create_event(
            event_type,
            data=data or {},
            workspace_id=workspace_id,
            user_id=user_id,
        )
        return get_dispatcher().dispatch(event)
    except Exception as exc:
        logger.error("Failed to emit event %s: %s", event_type, exc, exc_info=True)
        return {}
