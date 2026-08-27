"""Event system for plugins and application integration.

Provides centralized event dispatch with plugin subscription support.
Failures in event handlers are isolated - one failure doesn't affect others.
"""

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

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
            self.timestamp = datetime.now(timezone.utc)

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
            plugin_id: Optional plugin identifier for tracking/logging

        Raises:
            EventError: If event type is not supported
        """
        if event_type not in SUPPORTED_EVENTS:
            raise EventError(f"Unsupported event type: {event_type}")

        if event_type not in self._subscribers:
            self._subscribers[event_type] = []

        self._subscribers[event_type].append((plugin_id or "unknown", handler))
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

        Handler failures are isolated. If one handler fails, others still run.

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
                "errors": list of (plugin_id, exception) tuples
            }
        """
        handlers = self._subscribers.get(event.event_type, [])
        results = {
            "event_type": event.event_type,
            "total_handlers": len(handlers),
            "successful": 0,
            "failed": 0,
            "errors": [],
        }

        for plugin_id, handler in handlers:
            try:
                handler(event=event)
                results["successful"] += 1
            except Exception as e:
                results["failed"] += 1
                results["errors"].append((plugin_id, e))
                logger.error(
                    f"Error dispatching {event.event_type} to plugin {plugin_id}: {e}",
                    exc_info=True,
                )
                if raise_on_error:
                    raise

        return results

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
            Dictionary mapping event types to list of plugin IDs
        """
        if event_type:
            if event_type not in self._subscribers:
                return {event_type: []}
            plugin_ids = [plugin_id for plugin_id, _ in self._subscribers[event_type]]
            return {event_type: plugin_ids}

        result = {}
        for event_t, handlers in self._subscribers.items():
            result[event_t] = [plugin_id for plugin_id, _ in handlers]
        return result

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
        return f"<EventDispatcher with {len(self._subscribers)} event types, {self.get_subscription_count()} subscriptions>"


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
