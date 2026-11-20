"""Distributed tracing tests (#703).

Covers request-id propagation across two real provider service calls and a real
plugin dispatch, plus the attribute/span bounds and the response trace header.
"""

import json

from flask import g

from app.models import Plugin, PluginInstallation, User, Workspace
from app.observability.tracing import (
    MAX_SPAN_ATTRIBUTES,
    MAX_SPANS_PER_TRACE,
    TRACE_ID_HEADER,
    current_spans,
    reset_tracing,
    trace_span,
)
from app.services.events import EventDispatcher, create_event
from app.services.providers.mock import MockProvider
from app.services.providers.retry import RetryingProvider


def _seed_plugin(db):
    user = User(username="tracer", email="tracer@example.com")
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    workspace = Workspace(user_id=user.id, name="Trace workspace")
    db.session.add(workspace)
    db.session.commit()
    plugin = Plugin(
        id="trace-plugin",
        name="Trace plugin",
        version="1.0.0",
        entry_point="plugins.trace:TracePlugin",
    )
    db.session.add(plugin)
    db.session.commit()
    db.session.add(PluginInstallation(plugin_id=plugin.id, workspace_id=workspace.id))
    db.session.commit()
    return workspace, plugin


def test_request_id_propagates_across_service_calls_and_a_plugin_call(app, db, monkeypatch):
    reset_tracing()
    workspace, plugin = _seed_plugin(db)
    dispatcher = EventDispatcher()
    dispatcher.subscribe("project.created", lambda event: None, plugin_id=plugin.id)
    monkeypatch.setattr(dispatcher, "_authorize_plugin", lambda *_args: (True, "test"))

    with app.test_request_context("/"):
        g.request_id = "trace-test-123"
        provider = RetryingProvider(MockProvider())
        provider.chat([{"role": "user", "content": "one"}])
        provider.chat([{"role": "user", "content": "two"}])
        dispatcher.dispatch(create_event("project.created", workspace_id=workspace.id))

        spans = current_spans()

    assert [span.name for span in spans] == [
        "llm.provider.chat",
        "llm.provider.chat",
        "plugin.handler",
    ]
    assert {span.trace_id for span in spans} == {"trace-test-123"}
    plugin_span = spans[-1]
    assert plugin_span.attributes["plugin_id"] == "trace-plugin"
    assert plugin_span.attributes["event_type"] == "project.created"


def test_attributes_are_bounded_and_sensitive_names_are_dropped(app):
    reset_tracing()
    with app.test_request_context("/"):
        g.request_id = "trace-attrs"
        with trace_span(
            "bounded",
            prompt="do not record this prompt",
            api_key="sk-secret",
            authorization="Bearer secret",
            provider="mock",
            model="mock-1",
            attempt=1,
            **{f"field_{index}": "x" * 200 for index in range(20)},
        ):
            pass
        span = current_spans()[0]

    assert len(span.attributes) <= MAX_SPAN_ATTRIBUTES
    assert "prompt" not in span.attributes
    assert "api_key" not in span.attributes
    assert "authorization" not in span.attributes
    assert span.attributes["provider"] == "mock"
    assert all(len(value) <= 64 for value in span.attributes.values())


def test_span_count_per_trace_is_capped(app):
    reset_tracing()
    with app.test_request_context("/"):
        g.request_id = "trace-cap"
        for index in range(MAX_SPANS_PER_TRACE + 8):
            with trace_span("work", index=index):
                pass
        spans = current_spans()

    assert len(spans) == MAX_SPANS_PER_TRACE


def test_tracing_can_be_disabled(app):
    reset_tracing()
    app.config["TRACING_ENABLED"] = False
    try:
        with app.test_request_context("/"):
            g.request_id = "trace-off"
            with trace_span("ignored"):
                pass
            assert current_spans() == []
    finally:
        app.config["TRACING_ENABLED"] = True


def test_request_and_trace_headers_match(client):
    response = client.get("/health", headers={"X-Request-ID": "req-header-1"})

    assert response.status_code == 200
    assert response.headers["X-Request-ID"] == "req-header-1"
    assert response.headers[TRACE_ID_HEADER] == "req-header-1"


def test_span_record_is_json_serializable_with_a_stable_shape(app):
    reset_tracing()
    with app.test_request_context("/"):
        g.request_id = "trace-shape"
        with trace_span("service.call", provider="mock"):
            pass
        record = current_spans()[0].to_dict()

    assert json.loads(json.dumps(record))["event"] == "span"
    assert set(record) == {
        "event",
        "trace_id",
        "span_id",
        "parent_span_id",
        "name",
        "duration_ms",
        "attributes",
    }
