"""Security tests for dispatch-time plugin capability enforcement (#164).

Proves that a plugin handler only runs when the plugin exists, is enabled, is
installed for the event's workspace, holds the capability required by the
event through an explicit per-workspace grant, and the emitting user is
authorized for the workspace. Everything else fails closed and is recorded as
a denial - never as a handler run and never as a crash.
"""

import pytest

from app.extensions import db
from app.models import CapabilityGrant, Plugin, PluginInstallation, Project, Workspace
from app.services.capabilities import CapabilityStore
from app.services.events import (
    EVENT_CAPABILITY_MAP,
    GLOBAL_EVENTS,
    SUPPORTED_EVENTS,
    Event,
    EventDispatcher,
    EventError,
    emit_event,
    get_dispatcher,
)


def _workspace(owner, name="WS"):
    ws = Workspace(user_id=owner.id, name=name)
    db.session.add(ws)
    db.session.commit()
    return ws


def _project(ws, owner, name="P"):
    project = Project(
        workspace_id=ws.id,
        user_id=owner.id,
        name=name,
        source="archive",
        status="ready",
    )
    db.session.add(project)
    db.session.commit()
    return project


def _plugin(plugin_id, capabilities=("PROJECT_READ",), enabled=True):
    plugin = Plugin(
        id=plugin_id,
        name=plugin_id,
        version="0.1.0",
        description="test",
        author="tester",
        entry_point="plugins.test:TestPlugin",
        capabilities=list(capabilities),
        enabled=enabled,
    )
    db.session.add(plugin)
    db.session.commit()
    return plugin


def _install(plugin_id, workspace_id, enabled=True):
    installation = PluginInstallation(
        plugin_id=plugin_id,
        workspace_id=workspace_id,
        enabled=enabled,
    )
    db.session.add(installation)
    db.session.commit()
    return installation


def _grant(plugin_id, workspace_id, capability):
    CapabilityStore.grant(plugin_id, workspace_id, capability)


def _authorized(plugin_id, workspace_id, capability):
    _plugin(plugin_id)
    _install(plugin_id, workspace_id)
    _grant(plugin_id, workspace_id, capability)


def _subscribe(dispatcher, event_type, plugin_id, store):
    def handler(event):
        store.append(event)

    dispatcher.subscribe(event_type, handler, plugin_id=plugin_id)
    return handler


def _dispatch(dispatcher, event_type, workspace_id=None, user_id=None, data=None):
    return dispatcher.dispatch(
        Event(event_type=event_type, workspace_id=workspace_id, user_id=user_id, data=data)
    )


class TestAuthorizedDispatch:
    def test_authorized_plugin_receives_event(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        project = _project(ws, user)
        _authorized("plugin-a", ws.id, "project:read")

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "project.created", "plugin-a", received)
        result = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": project.id},
        )

        assert len(received) == 1
        assert result["successful"] == 1
        assert result["denied"] == 0

    def test_unauthorized_plugin_denied(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        project = _project(ws, user)
        # Plugin is installed but granted the wrong capability for this event.
        _authorized("plugin-a", ws.id, "workspace:read")

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "project.created", "plugin-a", received)
        result = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": project.id},
        )

        assert len(received) == 0
        assert result["denied"] == 1
        assert result["denials"][0][0] == "plugin-a"
        assert "grant" in result["denials"][0][1]

    def test_missing_workspace_context_denied(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        _authorized("plugin-a", ws.id, "project:read")

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "project.created", "plugin-a", received)
        # No workspace_id -> cannot prove authorization -> denied, no crash.
        result = _dispatch(
            dispatcher,
            "project.created",
            user_id=user.id,
            data={"project_id": 1},
        )

        assert len(received) == 0
        assert result["denied"] == 1
        assert "workspace" in result["denials"][0][1]

    def test_unknown_workspace_denied(self, app, make_user):
        user = make_user()
        _authorized("plugin-a", 99999, "project:read")

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "project.created", "plugin-a", received)
        result = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=99999,
            user_id=user.id,
            data={"project_id": 1},
        )

        assert len(received) == 0
        assert result["denied"] == 1
        assert "workspace" in result["denials"][0][1]


class TestPluginState:
    def test_disabled_plugin_denied(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        project = _project(ws, user)
        _plugin("plugin-a", enabled=False)
        _install("plugin-a", ws.id)
        _grant("plugin-a", ws.id, "project:read")

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "project.created", "plugin-a", received)
        result = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": project.id},
        )

        assert len(received) == 0
        assert result["denied"] == 1
        assert "disabled" in result["denials"][0][1]

    def test_disabled_installation_denied(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        project = _project(ws, user)
        _plugin("plugin-a")
        _install("plugin-a", ws.id, enabled=False)
        _grant("plugin-a", ws.id, "project:read")

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "project.created", "plugin-a", received)
        result = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": project.id},
        )

        assert len(received) == 0
        assert result["denied"] == 1
        assert "installation disabled" in result["denials"][0][1]

    def test_reenabled_plugin_dispatch_allowed(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        project = _project(ws, user)
        plugin = _plugin("plugin-a", enabled=False)
        _install("plugin-a", ws.id)
        _grant("plugin-a", ws.id, "project:read")

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "project.created", "plugin-a", received)

        # Disabled: denied.
        first = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": project.id},
        )
        assert first["denied"] == 1
        assert len(received) == 0

        # Re-enable: dispatch again, grant still valid.
        plugin.enabled = True
        db.session.commit()
        second = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": project.id},
        )
        assert second["denied"] == 0
        assert second["successful"] == 1
        assert len(received) == 1


class TestWorkspaceIsolation:
    def test_wrong_workspace_grant_denied(self, app, make_user):
        user = make_user()
        ws_a = _workspace(user, "A")
        ws_b = _workspace(user, "B")
        project_b = _project(ws_b, user)
        # Granted only in workspace A.
        _authorized("plugin-a", ws_a.id, "project:read")

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "project.created", "plugin-a", received)
        result = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=ws_b.id,
            user_id=user.id,
            data={"project_id": project_b.id},
        )

        assert len(received) == 0
        assert result["denied"] == 1
        # Not installed in workspace B.
        assert "installed" in result["denials"][0][1]

    def test_plugin_cannot_use_another_plugins_grant(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        project = _project(ws, user)
        # Plugin B holds the grant; plugin A does not.
        _plugin("plugin-a")
        _install("plugin-a", ws.id)
        _authorized("plugin-b", ws.id, "project:read")

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "project.created", "plugin-a", received)
        result = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": project.id},
        )

        assert len(received) == 0
        assert result["denied"] == 1
        assert "grant" in result["denials"][0][1]

    def test_workspace_a_grant_cannot_authorize_workspace_b(self, app, make_user):
        user = make_user()
        ws_a = _workspace(user, "A")
        ws_b = _workspace(user, "B")
        project_b = _project(ws_b, user)
        # Plugin A: installed and granted in A only.
        _authorized("plugin-a", ws_a.id, "project:read")

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "project.created", "plugin-a", received)
        result = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=ws_b.id,
            user_id=user.id,
            data={"project_id": project_b.id},
        )

        assert len(received) == 0
        assert result["denied"] == 1

    def test_project_mismatch_denied_confused_deputy(self, app, make_user):
        user = make_user()
        ws_a = _workspace(user, "A")
        ws_b = _workspace(user, "B")
        project_b = _project(ws_b, user, "P-B")
        # Plugin is authorized in workspace A; event claims workspace A but the
        # referenced project belongs to workspace B -> denied.
        _authorized("plugin-a", ws_a.id, "project:read")

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "project.created", "plugin-a", received)
        result = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=ws_a.id,
            user_id=user.id,
            data={"project_id": project_b.id},
        )

        assert len(received) == 0
        assert result["denied"] == 1
        assert "mismatch" in result["denials"][0][1]


class TestHandlerIsolation:
    def test_handler_failure_isolated_and_other_plugin_runs(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        project = _project(ws, user)
        _authorized("bad-plugin", ws.id, "project:read")
        _authorized("good-plugin", ws.id, "project:read")

        dispatcher = EventDispatcher()
        good_received = []

        def bad_handler(event):
            raise RuntimeError("boom")

        dispatcher.subscribe("project.created", bad_handler, plugin_id="bad-plugin")

        def good_handler(event):
            good_received.append(event)

        dispatcher.subscribe("project.created", good_handler, plugin_id="good-plugin")

        result = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": project.id},
        )

        assert len(good_received) == 1
        assert result["successful"] == 1
        assert result["failed"] == 1
        assert result["denied"] == 0
        assert result["errors"][0][0] == "bad-plugin"


class TestFailClosed:
    def test_unknown_event_denied(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        _authorized("plugin-a", ws.id, "project:read")

        # Plugins cannot even subscribe to an unknown event type.
        dispatcher = EventDispatcher()
        with pytest.raises(EventError):
            dispatcher.subscribe("no.such.event", lambda event: None, plugin_id="plugin-a")

        # Dispatching an unknown event type yields no delivery, no crash.
        result = dispatcher.dispatch(Event(event_type="no.such.event"))
        assert result["total_handlers"] == 0
        assert result["successful"] == 0
        assert result["denied"] == 0

    def test_unknown_plugin_denied(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        project = _project(ws, user)

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "project.created", "ghost-plugin", received)
        result = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": project.id},
        )

        assert len(received) == 0
        assert result["denied"] == 1
        assert "unknown plugin" in result["denials"][0][1]

    def test_missing_grant_denied(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        project = _project(ws, user)
        # Installed and enabled but never granted any capability.
        _plugin("plugin-a")
        _install("plugin-a", ws.id)

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "project.created", "plugin-a", received)
        result = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": project.id},
        )

        assert len(received) == 0
        assert result["denied"] == 1
        assert "grant" in result["denials"][0][1]

    def test_emitting_user_not_authorized_denied(self, app, make_user):
        user = make_user()
        other = make_user(username="other", email="other@example.com")
        ws = _workspace(user)
        project = _project(ws, user)
        _authorized("plugin-a", ws.id, "project:read")

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "project.created", "plugin-a", received)
        # The emitter is not the owner and not a member -> denied.
        result = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=ws.id,
            user_id=other.id,
            data={"project_id": project.id},
        )

        assert len(received) == 0
        assert result["denied"] == 1
        assert "user not authorized" in result["denials"][0][1]

    def test_no_capability_created_during_dispatch(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        project = _project(ws, user)
        _plugin("plugin-a")
        _install("plugin-a", ws.id)

        dispatcher = EventDispatcher()
        _subscribe(dispatcher, "project.created", "plugin-a", [])
        result = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": project.id},
        )
        assert result["denied"] == 1

        grants = CapabilityGrant.query.filter_by(
            plugin_id="plugin-a",
            workspace_id=ws.id,
        ).all()
        assert len(grants) == 0


class TestEventCapabilityMapping:
    def test_mapping_covers_every_supported_event(self):
        assert set(EVENT_CAPABILITY_MAP) == set(SUPPORTED_EVENTS)

    def test_stellar_analysis_requires_stellar_read(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        project = _project(ws, user)
        _authorized("stellar-plugin", ws.id, "stellar:read")

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "stellar.analysis.completed", "stellar-plugin", received)
        result = _dispatch(
            dispatcher,
            "stellar.analysis.completed",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": project.id, "detected": True},
        )
        assert len(received) == 1
        assert result["successful"] == 1

        # A plugin without stellar:read is denied.
        _authorized("plain-plugin", ws.id, "project:read")
        plain_received = []
        _subscribe(dispatcher, "stellar.analysis.completed", "plain-plugin", plain_received)
        result = _dispatch(
            dispatcher,
            "stellar.analysis.completed",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": project.id, "detected": True},
        )
        assert len(plain_received) == 0
        assert "grant" in result["denials"][-1][1]

    def test_ai_analysis_requires_ai_access(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        project = _project(ws, user)
        _authorized("ai-plugin", ws.id, "ai:access")

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "ai.analysis.completed", "ai-plugin", received)
        result = _dispatch(
            dispatcher,
            "ai.analysis.completed",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": project.id, "kind": "architecture"},
        )
        assert len(received) == 1
        assert result["successful"] == 1

        _authorized("plain-plugin", ws.id, "project:read")
        plain_received = []
        _subscribe(dispatcher, "ai.analysis.completed", "plain-plugin", plain_received)
        result = _dispatch(
            dispatcher,
            "ai.analysis.completed",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": project.id, "kind": "architecture"},
        )
        assert len(plain_received) == 0
        assert "grant" in result["denials"][-1][1]

    def test_global_event_delivered_without_workspace(self, app, make_user):
        user = make_user()
        _plugin("gh-plugin", capabilities=["GITHUB_READ"])

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "github.connected", "gh-plugin", received)
        result = _dispatch(
            dispatcher,
            "github.connected",
            user_id=user.id,
            data={"github_username": "alice"},
        )
        assert len(received) == 1
        assert result["successful"] == 1

    def test_global_event_denied_for_unknown_plugin(self, app, make_user):
        user = make_user()
        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "github.connected", "ghost", received)
        result = _dispatch(
            dispatcher,
            "github.connected",
            user_id=user.id,
            data={"github_username": "alice"},
        )
        assert len(received) == 0
        assert result["denied"] == 1
        assert "unknown plugin" in result["denials"][0][1]

    def test_global_events_are_whitelisted(self):
        assert {"github.connected", "github.disconnected"} == GLOBAL_EVENTS


class TestInternalHandlers:
    def test_internal_handler_not_subject_to_capability_checks(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        project = _project(ws, user)

        dispatcher = EventDispatcher()
        received = []
        # No plugin_id -> internal (trusted) handler, always delivered.
        dispatcher.subscribe("project.created", lambda event: received.append(event))
        result = _dispatch(
            dispatcher,
            "project.created",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": project.id},
        )
        assert len(received) == 1
        assert result["successful"] == 1


class TestProjectDeletedContext:
    def test_project_deleted_does_not_require_project_row(self, app, make_user):
        # project.deleted is emitted after the project row is removed; the
        # workspace binding is trusted from the owner-scoped route.
        user = make_user()
        ws = _workspace(user)
        _authorized("plugin-a", ws.id, "project:read")

        dispatcher = EventDispatcher()
        received = []
        _subscribe(dispatcher, "project.deleted", "plugin-a", received)
        result = _dispatch(
            dispatcher,
            "project.deleted",
            workspace_id=ws.id,
            user_id=user.id,
            data={"project_id": 424242},
        )
        assert len(received) == 1
        assert result["successful"] == 1


class TestEmitEventIntegration:
    def test_emit_event_delivers_to_authorized_plugin(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        project = _project(ws, user)
        _authorized("plugin-a", ws.id, "project:read")

        dispatcher = get_dispatcher()
        received = []
        _subscribe(dispatcher, "project.created", "plugin-a", received)
        emit_event(
            "project.created",
            data={"project_id": project.id},
            workspace_id=ws.id,
            user_id=user.id,
        )
        assert len(received) == 1

    def test_emit_event_never_raises_when_denied(self, app, make_user):
        user = make_user()
        ws = _workspace(user)
        _plugin("plugin-a")
        _install("plugin-a", ws.id)

        dispatcher = get_dispatcher()
        _subscribe(dispatcher, "project.created", "plugin-a", [])
        # Denied (no grant) but emit_event must not raise.
        result = emit_event(
            "project.created",
            data={"project_id": 1},
            workspace_id=ws.id,
            user_id=user.id,
        )
        assert result["denied"] == 1
