"""Tests for structured plugin error reporting (#173).

Covers bounded recording (dispatch handler failures, registry lifecycle hooks),
safe message/exception metadata (no stack traces, no payloads), owner-scoped
read endpoint authorization, operator CLI retrieval, and that recording never
changes failure-isolation behavior.
"""

import json
import types

import pytest

from app.extensions import db
from app.models import Plugin, PluginErrorReport, PluginInstallation, Project, Workspace
from app.models.workspace_member import WorkspaceMember
from app.services.capabilities import CapabilityStore
from app.services.events import Event, get_dispatcher
from app.services.plugin_errors import (
    MAX_ERROR_MESSAGE_CHARS,
    list_workspace_error_reports,
    record_plugin_error,
)
from app.services.plugins import Plugin as RegistryPlugin
from app.services.plugins import PluginManifest, PluginRegistry


def _runner(app):
    return app.test_cli_runner()


def _manifest(plugin_id):
    return PluginManifest(
        id=plugin_id,
        name="Test Plugin",
        version="0.1.0",
        description="test",
        author="tester",
        entry_point="plugins.test:TestPlugin",
        capabilities=["PROJECT_READ"],
    )


@pytest.fixture()
def workspace(app, make_user, login):
    user = make_user()
    login()
    ws = Workspace(user_id=user.id, name="Errors workspace")
    db.session.add(ws)
    db.session.commit()
    return ws


def _authorize_plugin(plugin_id, workspace_id):
    plugin = Plugin(
        id=plugin_id,
        name="Observer",
        version="0.1.0",
        description="test",
        author="tester",
        entry_point="plugins.test:TestPlugin",
        capabilities=["PROJECT_READ"],
    )
    db.session.add(plugin)
    db.session.commit()
    db.session.add(PluginInstallation(plugin_id=plugin_id, workspace_id=workspace_id))
    db.session.commit()
    CapabilityStore.grant(plugin_id, workspace_id, "project:read")


class TestRecording:
    def test_record_workspace_error(self, app, db):
        report = record_plugin_error(
            "some-plugin",
            "dispatch:project.created",
            exc=RuntimeError("boom"),
            workspace_id=7,
        )
        assert report is not None
        assert report.plugin_id == "some-plugin"
        assert report.operation == "dispatch:project.created"
        assert report.exception_type.endswith("RuntimeError")
        assert report.message == "boom"
        assert report.workspace_id == 7

    def test_explicit_safe_message_overrides_exception(self, app, db):
        report = record_plugin_error(
            "p", "hook:on_enable", exc=ValueError("secret leak"), message="safe reason"
        )
        assert report.message == "safe reason"
        assert "secret" not in report.message

    def test_message_is_truncated(self, app, db):
        long_message = "x" * (MAX_ERROR_MESSAGE_CHARS * 2)
        report = record_plugin_error("p", "op", message=long_message)
        assert len(report.message) == MAX_ERROR_MESSAGE_CHARS

    def test_no_stack_or_payload_stored(self, app, db):
        exc = ValueError("boom")
        report = record_plugin_error("p", "op", exc=exc)
        row = db.session.get(PluginErrorReport, report.id)
        serialized = row.to_dict()
        assert "Traceback" not in (serialized["message"] or "")
        assert set(serialized.keys()) == {
            "id",
            "workspace_id",
            "plugin_id",
            "operation",
            "exception_type",
            "message",
            "created_at",
        }


class TestDispatchHandlerErrors:
    def test_failing_handler_is_recorded_and_isolated(self, app, workspace):
        owner = db.session.get(Workspace, workspace.id)
        user_id = owner.user_id
        _authorize_plugin("bad-observer", workspace.id)
        project = Project(
            workspace_id=workspace.id,
            user_id=user_id,
            name="p",
            source="archive",
            status="ready",
        )
        db.session.add(project)
        db.session.commit()

        dispatcher = get_dispatcher()
        ran = []

        def good_handler(event):
            ran.append(event)

        def bad_handler(event):
            raise RuntimeError("plugin boom")

        dispatcher.subscribe("project.created", bad_handler, plugin_id="bad-observer")
        dispatcher.subscribe("project.created", good_handler)
        try:
            result = dispatcher.dispatch(
                Event(
                    "project.created",
                    data={"project_id": project.id},
                    workspace_id=workspace.id,
                    user_id=user_id,
                )
            )
        finally:
            dispatcher.unsubscribe("project.created", bad_handler)
            dispatcher.unsubscribe("project.created", good_handler)

        assert result["failed"] == 1
        assert result["successful"] == 1
        assert len(ran) == 1  # isolation preserved

        reports = list_workspace_error_reports(workspace.id)
        assert len(reports) == 1
        assert reports[0]["plugin_id"] == "bad-observer"
        assert reports[0]["operation"] == "dispatch:project.created"
        assert reports[0]["workspace_id"] == workspace.id
        assert reports[0]["exception_type"].endswith("RuntimeError")


class TestRegistryHookErrors:
    def test_hook_failure_recorded_when_in_app_context(self, app):
        registry = PluginRegistry()

        class FailingPlugin:
            def __init__(self, app=None, manifest=None):
                pass

            def on_disable(self):
                raise RuntimeError("disable boom")

        module = types.ModuleType("lifecycle_mod")
        module.LifecyclePlugin = FailingPlugin
        plugin = RegistryPlugin(_manifest("hook-failer"))
        plugin.module = module
        registry.register(plugin)

        registry.disable("hook-failer")  # must not raise
        reports = PluginErrorReport.query.filter_by(plugin_id="hook-failer").all()
        assert len(reports) == 1
        assert reports[0].operation == "hook:on_disable"


class TestOwnerEndpoint:
    def _url(self, workspace_id):
        return f"/plugins/api/workspaces/{workspace_id}/plugin-errors"

    def test_owner_reads_workspace_errors(self, app, client, workspace):
        record_plugin_error(
            "p1", "dispatch:project.created", exc=ValueError("x"), workspace_id=workspace.id
        )
        resp = client.get(self._url(workspace.id))
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["count"] == 1
        assert data["errors"][0]["plugin_id"] == "p1"

    def test_other_workspace_errors_not_returned(
        self, app, client, workspace, make_user, login, db
    ):
        record_plugin_error("p2", "op", exc=ValueError("y"), workspace_id=workspace.id)
        other = make_user(username="owner", email="owner2@example.com")
        login(email="owner2@example.com")
        ws2 = Workspace(user_id=other.id, name="Other")
        db.session.add(ws2)
        db.session.commit()
        resp = client.get(self._url(ws2.id))
        assert resp.status_code == 200
        assert resp.get_json()["count"] == 0

    def test_member_cannot_read_errors(self, app, client, make_user, login, db):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        ws = Workspace(user_id=owner.id, name="Members")
        db.session.add(ws)
        db.session.commit()
        login(email="owner@example.com")
        record_plugin_error("p", "op", exc=ValueError("z"), workspace_id=ws.id)
        membership = WorkspaceMember(workspace_id=ws.id, user_id=member.id, role="contributor")
        db.session.add(membership)
        db.session.commit()
        login(email="member@example.com")
        assert client.get(self._url(ws.id)).status_code == 403

    def test_outsider_gets_404(self, app, client, make_user, login, db):
        owner = make_user(username="owner", email="owner@example.com")
        ws = Workspace(user_id=owner.id, name="Hidden")
        db.session.add(ws)
        db.session.commit()
        make_user(username="outsider", email="outsider@example.com")
        login(email="outsider@example.com")
        assert client.get(self._url(ws.id)).status_code == 404


class TestCliErrors:
    def test_errors_list_and_json(self, app):
        record_plugin_error("cli-err", "dispatch:ai.analysis.completed", exc=ValueError("bad"))
        result = _runner(app).invoke(args=["plugins", "errors"])
        assert result.exit_code == 0
        assert "cli-err" in result.output
        assert "dispatch:ai.analysis.completed" in result.output

        result = _runner(app).invoke(args=["plugins", "errors", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["count"] >= 1
        assert any(e["plugin_id"] == "cli-err" for e in data["errors"])

    def test_empty(self, app):
        result = _runner(app).invoke(args=["plugins", "errors"])
        assert result.exit_code == 0
        assert "No plugin error reports recorded." in result.output
