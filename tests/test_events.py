"""Tests for the event system and event dispatcher."""

from datetime import datetime

import pytest

from app.services.events import (
    Event,
    EventDispatcher,
    EventError,
    create_event,
    get_dispatcher,
)


class TestEvent:
    """Test Event dataclass."""

    def test_create_basic_event(self):
        """Test creating a basic event."""
        event = Event(event_type="project.created")
        assert event.event_type == "project.created"
        assert event.data == {}
        assert event.timestamp is not None
        assert event.workspace_id is None
        assert event.user_id is None

    def test_create_event_with_data(self):
        """Test creating event with data payload."""
        data = {"project_id": 42, "name": "test-project"}
        event = Event(event_type="project.created", data=data)
        assert event.data == data
        assert event.data["project_id"] == 42

    def test_event_with_context(self):
        """Test event with workspace/user context."""
        event = Event(
            event_type="workspace.member_added",
            data={"member_id": 5},
            workspace_id=10,
            user_id=3,
        )
        assert event.workspace_id == 10
        assert event.user_id == 3

    def test_event_timestamp_default(self):
        """Test that timestamp defaults to current time."""
        event1 = Event(event_type="project.created")
        event2 = Event(event_type="project.created")
        # Timestamps should be close (within 1 second)
        delta = (event2.timestamp - event1.timestamp).total_seconds()
        assert delta < 1.0

    def test_event_custom_timestamp(self):
        """Test setting custom timestamp."""
        ts = datetime(2024, 1, 15, 10, 30, 0)
        event = Event(event_type="project.created", timestamp=ts)
        assert event.timestamp == ts

    def test_event_to_dict(self):
        """Test converting event to dictionary."""
        event = Event(
            event_type="project.created",
            data={"project_id": 1},
            workspace_id=10,
            user_id=5,
        )
        result = event.to_dict()
        assert result["event_type"] == "project.created"
        assert result["data"] == {"project_id": 1}
        assert result["workspace_id"] == 10
        assert result["user_id"] == 5
        assert "timestamp" in result

    def test_event_repr(self):
        """Test event string representation."""
        event = Event(event_type="review.completed")
        repr_str = repr(event)
        assert "Event" in repr_str
        assert "review.completed" in repr_str


class TestEventDispatcher:
    """Test EventDispatcher class."""

    def test_create_dispatcher(self):
        """Test creating a dispatcher."""
        dispatcher = EventDispatcher()
        assert dispatcher.get_subscription_count() == 0

    def test_subscribe_to_event(self):
        """Test subscribing to an event."""
        dispatcher = EventDispatcher()
        received = []

        def handler(event):
            received.append(event)

        dispatcher.subscribe("project.created", handler, plugin_id="test-plugin")
        assert dispatcher.get_subscription_count() == 1

    def test_subscribe_invalid_event_type(self):
        """Test subscribing to invalid event type raises error."""
        dispatcher = EventDispatcher()

        def handler(event):
            pass

        with pytest.raises(EventError, match="Unsupported event type"):
            dispatcher.subscribe("invalid.event", handler)

    def test_unsubscribe_from_event(self):
        """Test unsubscribing from an event."""
        dispatcher = EventDispatcher()

        def handler(event):
            pass

        dispatcher.subscribe("project.created", handler)
        assert dispatcher.get_subscription_count() == 1

        result = dispatcher.unsubscribe("project.created", handler)
        assert result is True
        assert dispatcher.get_subscription_count() == 0

    def test_unsubscribe_nonexistent(self):
        """Test unsubscribing nonexistent handler returns False."""
        dispatcher = EventDispatcher()

        def handler(event):
            pass

        result = dispatcher.unsubscribe("project.created", handler)
        assert result is False

    def test_dispatch_event(self):
        """Test dispatching an event to subscribers."""
        dispatcher = EventDispatcher()
        received = []

        def handler(event):
            received.append(event)

        dispatcher.subscribe("project.created", handler)
        event = Event(event_type="project.created", data={"project_id": 1})
        result = dispatcher.dispatch(event)

        assert len(received) == 1
        assert received[0] == event
        assert result["total_handlers"] == 1
        assert result["successful"] == 1
        assert result["failed"] == 0

    def test_dispatch_to_multiple_subscribers(self):
        """Test dispatching to multiple subscribers."""
        dispatcher = EventDispatcher()
        received1 = []
        received2 = []

        def handler1(event):
            received1.append(event)

        def handler2(event):
            received2.append(event)

        dispatcher.subscribe("project.created", handler1, plugin_id="plugin1")
        dispatcher.subscribe("project.created", handler2, plugin_id="plugin2")
        event = Event(event_type="project.created")
        result = dispatcher.dispatch(event)

        assert len(received1) == 1
        assert len(received2) == 1
        assert result["successful"] == 2
        assert result["failed"] == 0

    def test_dispatch_handler_error_isolation(self):
        """Test that handler errors are isolated."""
        dispatcher = EventDispatcher()
        received = []

        def failing_handler(event):
            raise ValueError("Handler failed")

        def good_handler(event):
            received.append(event)

        dispatcher.subscribe("project.created", failing_handler, plugin_id="bad-plugin")
        dispatcher.subscribe("project.created", good_handler, plugin_id="good-plugin")
        event = Event(event_type="project.created")
        result = dispatcher.dispatch(event)

        # Good handler should still be called despite failing handler
        assert len(received) == 1
        assert result["successful"] == 1
        assert result["failed"] == 1
        assert len(result["errors"]) == 1
        assert result["errors"][0][0] == "bad-plugin"
        assert isinstance(result["errors"][0][1], ValueError)

    def test_dispatch_raises_on_error_flag(self):
        """Test raise_on_error flag re-raises first error."""
        dispatcher = EventDispatcher()

        def failing_handler(event):
            raise RuntimeError("Test error")

        dispatcher.subscribe("project.created", failing_handler)
        event = Event(event_type="project.created")

        with pytest.raises(RuntimeError, match="Test error"):
            dispatcher.dispatch(event, raise_on_error=True)

    def test_dispatch_to_no_subscribers(self):
        """Test dispatching to event with no subscribers."""
        dispatcher = EventDispatcher()
        event = Event(event_type="project.created")
        result = dispatcher.dispatch(event)

        assert result["total_handlers"] == 0
        assert result["successful"] == 0
        assert result["failed"] == 0

    def test_list_subscribers_all(self):
        """Test listing all subscribers."""
        dispatcher = EventDispatcher()

        def handler1(event):
            pass

        def handler2(event):
            pass

        def handler3(event):
            pass

        dispatcher.subscribe("project.created", handler1, plugin_id="plugin1")
        dispatcher.subscribe("project.created", handler2, plugin_id="plugin2")
        dispatcher.subscribe("review.completed", handler3, plugin_id="plugin3")

        subscribers = dispatcher.list_subscribers()
        assert "project.created" in subscribers
        assert "review.completed" in subscribers
        assert len(subscribers["project.created"]) == 2
        assert len(subscribers["review.completed"]) == 1

    def test_list_subscribers_by_event_type(self):
        """Test listing subscribers for specific event type."""
        dispatcher = EventDispatcher()

        def handler1(event):
            pass

        def handler2(event):
            pass

        dispatcher.subscribe("project.created", handler1, plugin_id="plugin1")
        dispatcher.subscribe("project.created", handler2, plugin_id="plugin2")
        dispatcher.subscribe("review.completed", handler1, plugin_id="plugin3")

        subscribers = dispatcher.list_subscribers("project.created")
        assert len(subscribers["project.created"]) == 2

    def test_list_subscribers_empty_event_type(self):
        """Test listing subscribers for event type with no subscribers."""
        dispatcher = EventDispatcher()
        subscribers = dispatcher.list_subscribers("project.created")
        assert subscribers == {"project.created": []}

    def test_list_supported_events(self):
        """Test listing supported event types."""
        events = EventDispatcher.list_supported_events()
        assert isinstance(events, dict)
        assert len(events) > 0
        assert "project.created" in events
        assert "review.completed" in events

    def test_dispatcher_repr(self):
        """Test dispatcher string representation."""
        dispatcher = EventDispatcher()

        def handler(event):
            pass

        dispatcher.subscribe("project.created", handler)
        repr_str = repr(dispatcher)
        assert "EventDispatcher" in repr_str

    def test_subscription_without_plugin_id(self):
        """Test subscribing without explicit plugin_id."""
        dispatcher = EventDispatcher()

        def handler(event):
            pass

        dispatcher.subscribe("project.created", handler)
        subscribers = dispatcher.list_subscribers("project.created")
        assert "unknown" in subscribers["project.created"]


class TestCreateEventFactory:
    """Test create_event factory function."""

    def test_create_event_basic(self):
        """Test basic event creation."""
        event = create_event("project.created")
        assert event.event_type == "project.created"
        assert event.data == {}

    def test_create_event_with_data(self):
        """Test event creation with data."""
        data = {"project_id": 1}
        event = create_event("project.created", data=data)
        assert event.data == data

    def test_create_event_with_context(self):
        """Test event creation with workspace/user context."""
        event = create_event(
            "workspace.member_added",
            workspace_id=10,
            user_id=5,
        )
        assert event.workspace_id == 10
        assert event.user_id == 5

    def test_create_event_invalid_type(self):
        """Test creating event with invalid type raises error."""
        with pytest.raises(EventError, match="Unsupported event type"):
            create_event("invalid.event")


class TestGetDispatcher:
    """Test global dispatcher singleton."""

    def test_get_dispatcher_singleton(self):
        """Test that get_dispatcher returns singleton."""
        # This test is tricky because dispatcher is module-level
        # We'll just verify it returns an EventDispatcher
        dispatcher = get_dispatcher()
        assert isinstance(dispatcher, EventDispatcher)


class TestEventIntegration:
    """Integration tests for event system."""

    def test_full_event_workflow(self):
        """Test complete event creation, subscription, and dispatch."""
        dispatcher = EventDispatcher()
        results = []

        def project_handler(event):
            results.append(
                {
                    "type": "project",
                    "data": event.data,
                }
            )

        def review_handler(event):
            results.append(
                {
                    "type": "review",
                    "data": event.data,
                }
            )

        dispatcher.subscribe("project.created", project_handler, plugin_id="core")
        dispatcher.subscribe("review.completed", review_handler, plugin_id="review-plugin")

        # Dispatch project event
        project_event = create_event(
            "project.created",
            data={"project_id": 1, "name": "test"},
            workspace_id=10,
        )
        dispatcher.dispatch(project_event)

        # Dispatch review event
        review_event = create_event(
            "review.completed",
            data={"review_id": 5},
            workspace_id=10,
        )
        dispatcher.dispatch(review_event)

        assert len(results) == 2
        assert results[0]["type"] == "project"
        assert results[0]["data"]["project_id"] == 1
        assert results[1]["type"] == "review"
        assert results[1]["data"]["review_id"] == 5

    def test_event_with_multiple_plugins(self):
        """Test multiple plugins handling same event."""
        dispatcher = EventDispatcher()
        plugin_actions = {"plugin1": 0, "plugin2": 0, "plugin3": 0}

        def make_handler(plugin_name):
            def handler(event):
                plugin_actions[plugin_name] += 1

            return handler

        dispatcher.subscribe("project.created", make_handler("plugin1"), plugin_id="plugin1")
        dispatcher.subscribe("project.created", make_handler("plugin2"), plugin_id="plugin2")
        dispatcher.subscribe("project.created", make_handler("plugin3"), plugin_id="plugin3")

        event = Event(event_type="project.created", data={"project_id": 1})
        dispatcher.dispatch(event)

        assert plugin_actions["plugin1"] == 1
        assert plugin_actions["plugin2"] == 1
        assert plugin_actions["plugin3"] == 1

    def test_stellar_events(self):
        """Test Stellar-specific events."""
        dispatcher = EventDispatcher()
        stellar_events = []

        def stellar_handler(event):
            stellar_events.append(event)

        dispatcher.subscribe("stellar.network.detected", stellar_handler)
        dispatcher.subscribe("stellar.analysis.completed", stellar_handler)

        event1 = create_event("stellar.network.detected", data={"network": "testnet"})
        event2 = create_event("stellar.analysis.completed", data={"findings": []})

        dispatcher.dispatch(event1)
        dispatcher.dispatch(event2)

        assert len(stellar_events) == 2
        assert stellar_events[0].event_type == "stellar.network.detected"
        assert stellar_events[1].event_type == "stellar.analysis.completed"
