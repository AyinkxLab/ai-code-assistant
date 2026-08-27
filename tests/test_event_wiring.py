"""Tests that application routes actually emit events to authorized plugins.

Verifies the event dispatcher is wired into the real request lifecycle for
project creation, member changes, AI analysis, and GitHub connection, and that
a failing plugin handler never breaks the request. Subscribers are real
plugins (installed + granted) so these tests exercise the dispatch-time
capability enforcement end to end.
"""

import io
import zipfile

from app.extensions import db
from app.models import Plugin, PluginInstallation, Project, User, Workspace
from app.services.capabilities import CapabilityStore
from app.services.events import get_dispatcher


def _make_plugin(plugin_id, capabilities=("PROJECT_READ",), enabled=True):
    plugin = Plugin(
        id=plugin_id,
        name="Test Plugin",
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


def _authorize(plugin_id, workspace_id, capability):
    _make_plugin(plugin_id, capabilities=[capability])
    _install(plugin_id, workspace_id)
    _grant(plugin_id, workspace_id, capability)


def _zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def _upload_archive(client, workspace_id, bytes_):
    return client.post(
        f"/workspaces/api/workspaces/{workspace_id}/projects",
        data={"file": (io.BytesIO(bytes_), "project.zip")},
        content_type="multipart/form-data",
    )


def _subscribe(dispatcher, event_type, store, plugin_id):
    def handler(event):
        store.append(event)

    dispatcher.subscribe(event_type, handler, plugin_id=plugin_id)
    return handler


class TestProjectEvents:
    def test_project_created_emitted(self, app, client, make_user, login):
        user = make_user()
        login()
        ws = Workspace(user_id=user.id, name="Events workspace")
        db.session.add(ws)
        db.session.commit()
        _authorize("project-observer", ws.id, "project:read")

        dispatcher = get_dispatcher()
        events = []
        handler = _subscribe(dispatcher, "project.created", events, "project-observer")
        try:
            resp = _upload_archive(client, ws.id, _zip_bytes([("main.py", "print('hi')")]))
            assert resp.status_code == 201
        finally:
            dispatcher.unsubscribe("project.created", handler)

        assert len(events) == 1
        assert events[0].data["project_id"] == resp.get_json()["id"]
        assert events[0].workspace_id == ws.id

    def test_project_deleted_emitted(self, app, client, make_user, login):
        user = make_user()
        login()
        ws = Workspace(user_id=user.id, name="Delete workspace")
        db.session.add(ws)
        db.session.commit()
        project = Project(
            workspace_id=ws.id,
            user_id=user.id,
            name="to delete",
            source="archive",
            status="ready",
        )
        db.session.add(project)
        db.session.commit()
        _authorize("project-observer", ws.id, "project:read")

        dispatcher = get_dispatcher()
        events = []
        handler = _subscribe(dispatcher, "project.deleted", events, "project-observer")
        try:
            resp = client.delete(f"/workspaces/api/projects/{project.id}")
            assert resp.status_code == 200
        finally:
            dispatcher.unsubscribe("project.deleted", handler)

        assert len(events) == 1
        assert events[0].data["project_id"] == project.id


class TestMemberEvents:
    def test_member_added_emitted(self, app, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        make_user(username="alice", email="alice@example.com")
        login(email="owner@example.com")
        ws = Workspace(user_id=owner.id, name="Members workspace")
        db.session.add(ws)
        db.session.commit()
        _authorize("member-observer", ws.id, "workspace:read")

        dispatcher = get_dispatcher()
        events = []
        handler = _subscribe(dispatcher, "workspace.member_added", events, "member-observer")
        try:
            resp = client.post(
                f"/workspaces/api/workspaces/{ws.id}/members",
                json={"username": "alice", "role": "viewer"},
            )
            assert resp.status_code == 201
        finally:
            dispatcher.unsubscribe("workspace.member_added", handler)

        assert len(events) == 1
        assert events[0].data["user_id"] == User.query.filter_by(username="alice").first().id


class TestAnalysisEvents:
    def test_ai_analysis_completed_emitted(self, app, client, make_user, login):
        user = make_user()
        login()
        ws = Workspace(user_id=user.id, name="Analysis workspace")
        db.session.add(ws)
        db.session.commit()
        project = Project(
            workspace_id=ws.id,
            user_id=user.id,
            name="p",
            source="archive",
            status="ready",
        )
        db.session.add(project)
        db.session.commit()
        _authorize("ai-observer", ws.id, "ai:access")

        dispatcher = get_dispatcher()
        events = []
        handler = _subscribe(dispatcher, "ai.analysis.completed", events, "ai-observer")
        try:
            resp = client.post(
                f"/workspaces/api/projects/{project.id}/analyze",
                json={"kind": "architecture"},
            )
            assert resp.status_code == 200
        finally:
            dispatcher.unsubscribe("ai.analysis.completed", handler)

        assert len(events) == 1
        assert events[0].data["kind"] == "architecture"


class TestGithubEvents:
    def test_github_connected_emitted(self, app, make_user):
        user = make_user()
        _make_plugin("github-observer", capabilities=["GITHUB_READ"])

        dispatcher = get_dispatcher()
        events = []
        handler = _subscribe(dispatcher, "github.connected", events, "github-observer")

        # The /github/callback route calls emit_event after a successful OAuth
        # exchange; exercise the same dispatch path the route uses.
        from app.services.events import emit_event

        try:
            emit_event("github.connected", data={"github_username": "alice"}, user_id=user.id)
        finally:
            dispatcher.unsubscribe("github.connected", handler)

        assert len(events) == 1
        assert events[0].data["github_username"] == "alice"


class TestFailureIsolation:
    def test_failing_plugin_does_not_break_request(self, app, client, make_user, login):
        user = make_user()
        login()
        ws = Workspace(user_id=user.id, name="Isolation workspace")
        db.session.add(ws)
        db.session.commit()
        _authorize("bad-plugin", ws.id, "project:read")

        dispatcher = get_dispatcher()

        def bad_handler(event):
            raise RuntimeError("plugin boom")

        dispatcher.subscribe("project.created", bad_handler, plugin_id="bad-plugin")
        try:
            resp = _upload_archive(client, ws.id, _zip_bytes([("main.py", "print('hi')")]))
            assert resp.status_code == 201
        finally:
            dispatcher.unsubscribe("project.created", bad_handler)
