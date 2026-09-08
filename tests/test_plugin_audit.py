"""Tests for the plugin capability/state security audit trail (#192).

Every grant/revoke/enable/disable is recorded as an append-only ActivityEvent
with actor/workspace/plugin/capability/outcome; dispatch-time authorization
denials are recorded as outcomes (never payloads); plugin audit entries are
owner-visible only and isolated per workspace.
"""

from app.extensions import db
from app.models import ActivityEvent, Project, Workspace, WorkspaceMember
from app.models.activity_event import (
    AUDIT_EVENT_TYPES,
    EVENT_PLUGIN_CAPABILITY_GRANTED,
    EVENT_PLUGIN_CAPABILITY_REVOKED,
    EVENT_PLUGIN_DENIED,
    EVENT_PLUGIN_DISABLED,
    EVENT_PLUGIN_ENABLED,
)
from app.services.capabilities import CapabilityStore
from app.services.events import Event, get_dispatcher

MANIFEST = {
    "id": "test-plugin",
    "name": "Test Plugin",
    "version": "0.1.0",
    "description": "A test plugin",
    "author": "Tester",
    "entry_point": "plugins.test:TestPlugin",
    "capabilities": ["PROJECT_READ"],
}
SAFE_METADATA_KEYS = {
    "action",
    "outcome",
    "plugin_id",
    "capability",
    "reason",
    "trigger_event_type",
}


def _workspace(db_, owner, name="WS"):
    ws = Workspace(user_id=owner.id, name=name)
    db.session.add(ws)
    db.session.commit()
    return ws


def _member(db_, ws, user, role="contributor"):
    membership = WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=role)
    db.session.add(membership)
    db.session.commit()
    return membership


def _install(client, ws_id, manifest=MANIFEST):
    return client.post(
        f"/plugins/api/workspaces/{ws_id}/plugins/install", json={"manifest": manifest}
    )


def _capabilities_url(ws_id, plugin_id="test-plugin"):
    return f"/plugins/api/workspaces/{ws_id}/plugins/{plugin_id}/capabilities"


def _audit_url(ws_id):
    return f"/workspaces/api/workspaces/{ws_id}/audit"


def _plugin_events(workspace_id):
    return (
        ActivityEvent.query.filter(
            ActivityEvent.workspace_id == workspace_id,
            ActivityEvent.event_type.in_(AUDIT_EVENT_TYPES),
        )
        .order_by(ActivityEvent.id)
        .all()
    )


class TestActionRecording:
    def test_enable_disable_recorded(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        _install(client, ws.id)

        assert (
            client.post(f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/disable").status_code
            == 200
        )
        assert (
            client.post(f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/enable").status_code
            == 200
        )
        events = _plugin_events(ws.id)
        types = [e.event_type for e in events]
        assert types == [EVENT_PLUGIN_DISABLED, EVENT_PLUGIN_ENABLED]
        assert events[0].actor_id == owner.id
        assert events[0].event_metadata["plugin_id"] == "test-plugin"
        assert events[0].event_metadata["outcome"] == "success"

    def test_noop_enable_is_not_recorded(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        _install(client, ws.id)  # installation starts enabled
        for _ in range(2):
            client.post(f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/enable")
        enabled = [e for e in _plugin_events(ws.id) if e.event_type == EVENT_PLUGIN_ENABLED]
        assert enabled == []

    def test_grant_and_revoke_recorded(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        _install(client, ws.id)

        client.post(_capabilities_url(ws.id), json={"grant": ["PROJECT_READ"], "revoke": []})
        client.post(_capabilities_url(ws.id), json={"grant": [], "revoke": ["PROJECT_READ"]})
        events = _plugin_events(ws.id)
        types = [e.event_type for e in events]
        assert types == [EVENT_PLUGIN_CAPABILITY_GRANTED, EVENT_PLUGIN_CAPABILITY_REVOKED]
        granted = events[0]
        assert granted.actor_id == owner.id
        assert granted.event_metadata == {
            "action": "granted",
            "outcome": "success",
            "plugin_id": "test-plugin",
            "capability": "project:read",
        }
        assert events[1].event_metadata["capability"] == "project:read"

    def test_repeat_grant_records_single_event(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        _install(client, ws.id)
        for _ in range(2):
            client.post(
                _capabilities_url(ws.id),
                json={"grant": ["PROJECT_READ"], "revoke": []},
            )
        granted = [
            e for e in _plugin_events(ws.id) if e.event_type == EVENT_PLUGIN_CAPABILITY_GRANTED
        ]
        assert len(granted) == 1

    def test_undeclared_capability_request_is_denied_and_recorded(
        self, app, client, make_user, login
    ):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        _install(client, ws.id)
        response = client.post(
            _capabilities_url(ws.id),
            json={"grant": ["WORKSPACE_WRITE"], "revoke": []},
        )
        assert response.status_code == 400
        denied = [e for e in _plugin_events(ws.id) if e.event_type == EVENT_PLUGIN_DENIED]
        assert len(denied) == 1
        assert denied[0].actor_id == owner.id
        assert denied[0].event_metadata["outcome"] == "denied"
        assert denied[0].event_metadata["capability"] == "workspace:write"
        assert "not declared" in denied[0].event_metadata["reason"]

    def test_no_grant_row_created_for_denied_request(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        _install(client, ws.id)
        client.post(
            _capabilities_url(ws.id),
            json={"grant": ["WORKSPACE_WRITE"], "revoke": []},
        )
        assert CapabilityStore.list_capabilities("test-plugin", ws.id) == []


class TestDispatchDenialRecording:
    def _subscribed_workspace(self, app, make_user):
        from app.extensions import db as _db

        owner = make_user()
        ws = _workspace(_db, owner)
        project = Project(
            workspace_id=ws.id,
            user_id=owner.id,
            name="p",
            source="archive",
            status="ready",
        )
        db.session.add(project)
        db.session.commit()
        return owner, ws, project

    def test_disabled_installation_denial_recorded(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        project = Project(
            workspace_id=ws.id,
            user_id=owner.id,
            name="p",
            source="archive",
            status="ready",
        )
        db.session.add(project)
        db.session.commit()
        _install(client, ws.id)
        CapabilityStore.grant("test-plugin", ws.id, "project:read")

        dispatcher = get_dispatcher()
        calls = []

        def handler(event):
            calls.append(event)

        dispatcher.subscribe("project.created", handler, plugin_id="test-plugin")
        try:
            client.post(f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/disable")
            event = Event(
                event_type="project.created",
                workspace_id=ws.id,
                user_id=owner.id,
                data={"project_id": project.id},
            )
            result = dispatcher.dispatch(event)
            assert result["denied"] == 1
            assert len(calls) == 0
        finally:
            dispatcher.unsubscribe("project.created", handler)

        denied = [e for e in _plugin_events(ws.id) if e.event_type == EVENT_PLUGIN_DENIED]
        assert len(denied) == 1
        assert denied[0].event_metadata["outcome"] == "denied"
        assert denied[0].event_metadata["plugin_id"] == "test-plugin"
        assert denied[0].event_metadata["capability"] == "project:read"
        assert denied[0].event_metadata["reason"] == "plugin installation disabled"
        assert denied[0].event_metadata["trigger_event_type"] == "project.created"

    def test_missing_capability_grant_denial_recorded(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        project = Project(
            workspace_id=ws.id,
            user_id=owner.id,
            name="p",
            source="archive",
            status="ready",
        )
        db.session.add(project)
        db.session.commit()
        _install(client, ws.id)

        dispatcher = get_dispatcher()
        calls = []

        def handler(event):
            calls.append(event)

        dispatcher.subscribe("project.created", handler, plugin_id="test-plugin")
        try:
            event = Event(
                event_type="project.created",
                workspace_id=ws.id,
                user_id=owner.id,
                data={"project_id": project.id},
            )
            result = dispatcher.dispatch(event)
            assert result["denied"] == 1
            assert len(calls) == 0
        finally:
            dispatcher.unsubscribe("project.created", handler)

        denied = [e for e in _plugin_events(ws.id) if e.event_type == EVENT_PLUGIN_DENIED]
        assert denied and denied[0].event_metadata["reason"] == "missing capability grant"


class TestAuditVisibilityAndIsolation:
    def test_owner_sees_plugin_events_with_metadata(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        _install(client, ws.id)
        client.post(f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/disable")

        response = client.get(_audit_url(ws.id))
        assert response.status_code == 200
        items = response.get_json()["items"]
        types = {item["event_type"] for item in items}
        assert EVENT_PLUGIN_DISABLED in types
        matching = [item for item in items if item["event_type"] == EVENT_PLUGIN_DISABLED]
        assert matching[0]["actor_username"] == owner.username
        assert matching[0]["metadata"]["plugin_id"] == "test-plugin"

    def test_plugin_audit_metadata_never_leaks_payloads(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        _install(client, ws.id)
        client.post(_capabilities_url(ws.id), json={"grant": ["PROJECT_READ"], "revoke": []})
        client.post(f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/disable")
        for event in _plugin_events(ws.id):
            keys = set((event.event_metadata or {}).keys())
            assert keys.issubset(SAFE_METADATA_KEYS)

    def test_member_cannot_read_audit(self, app, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        ws = _workspace(db, owner)
        _member(db, ws, member, role="contributor")
        login(email="member@example.com")
        assert client.get(_audit_url(ws.id)).status_code == 403

    def test_non_member_gets_404(self, app, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        make_user(username="outsider", email="outsider@example.com")
        ws = _workspace(db, owner)
        _install(client, ws.id)
        login(email="outsider@example.com")
        assert client.get(_audit_url(ws.id)).status_code == 404

    def test_cross_workspace_isolation(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws_a = _workspace(db, owner, "A")
        ws_b = _workspace(db, owner, "B")
        _install(client, ws_a.id)
        _install(client, ws_b.id)
        client.post(f"/plugins/api/workspaces/{ws_a.id}/plugins/test-plugin/disable")
        client.post(f"/plugins/api/workspaces/{ws_b.id}/plugins/test-plugin/disable")

        audit_a = client.get(_audit_url(ws_a.id)).get_json()["items"]
        audit_b = client.get(_audit_url(ws_b.id)).get_json()["items"]
        for item in audit_a:
            assert item["workspace_id"] == ws_a.id
        for item in audit_b:
            assert item["workspace_id"] == ws_b.id
        assert len(audit_a) == 1 and len(audit_b) == 1

    def test_member_activity_feed_excludes_plugin_audit(self, app, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        ws = _workspace(db, owner)
        _install(client, ws.id)
        _member(db, ws, member, role="viewer")
        # Owner performs the auditable action.
        login(email="owner@example.com")
        client.post(f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/disable")
        # The member feed must not leak the audit-sensitive plugin event.
        login(email="member@example.com")
        feed = client.get(f"/workspaces/api/workspaces/{ws.id}/activity").get_json()
        types = {item["event_type"] for item in feed["items"]}
        assert not types.intersection(
            {
                EVENT_PLUGIN_CAPABILITY_GRANTED,
                EVENT_PLUGIN_CAPABILITY_REVOKED,
                EVENT_PLUGIN_ENABLED,
                EVENT_PLUGIN_DISABLED,
                EVENT_PLUGIN_DENIED,
            }
        )
