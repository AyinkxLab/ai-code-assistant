"""Tests for payload-free plugin execution audit logs (#690).

Every workspace-scoped callback records its plugin id, version, capability,
outcome, and duration. Payload contents and secrets are never stored, and the
log is readable only by workspace owners through the plugin API.
"""

import threading

from app.extensions import db
from app.models import ActivityEvent, Workspace, WorkspaceMember
from app.models import Plugin as PluginRow
from app.models.activity_event import EVENT_PLUGIN_EXECUTED
from app.services.events import Event, EventDispatcher
from app.services.pagination import encode_cursor
from app.services.plugin_audit import record_plugin_execution

SAFE_KEYS = {"action", "plugin_id", "version", "capability", "outcome", "duration_ms"}


def _workspace(owner, name="WS"):
    workspace = Workspace(user_id=owner.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _plugin(plugin_id="audit-plugin", version="2.4.1"):
    db.session.add(
        PluginRow(
            id=plugin_id,
            name="Audit plugin",
            version=version,
            entry_point="audit:Audit",
            capabilities=["PROJECT_READ"],
            enabled=True,
        )
    )
    db.session.commit()
    return plugin_id


def _execution_rows(workspace_id):
    return (
        ActivityEvent.query.filter_by(workspace_id=workspace_id, event_type=EVENT_PLUGIN_EXECUTED)
        .order_by(ActivityEvent.id)
        .all()
    )


def _dispatch(dispatcher, event_type="project.created", workspace_id=None):
    return dispatcher.dispatch(Event(event_type=event_type, workspace_id=workspace_id))


class TestExecutionRecording:
    def _dispatcher(self, monkeypatch):
        dispatcher = EventDispatcher()
        monkeypatch.setattr(dispatcher, "_authorize_plugin", lambda plugin_id, event: (True, ""))
        return dispatcher

    def test_success_records_only_safe_facts(self, app, monkeypatch):
        workspace = _workspace(_make_owner())
        plugin_id = _plugin()
        dispatcher = self._dispatcher(monkeypatch)
        dispatcher.subscribe(
            "project.created",
            lambda event: {"secret": "payload-sentinel"},
            plugin_id=plugin_id,
        )

        result = _dispatch(dispatcher, workspace_id=workspace.id)
        db.session.commit()

        assert result["successful"] == 1
        row = _execution_rows(workspace.id)[0]
        metadata = row.event_metadata
        assert set(metadata) == SAFE_KEYS
        assert metadata["plugin_id"] == plugin_id
        assert metadata["version"] == "2.4.1"
        assert metadata["capability"] == "project:read"
        assert metadata["outcome"] == "success"
        assert metadata["duration_ms"] >= 0
        assert "payload-sentinel" not in str(metadata)

    def test_execution_row_survives_session_teardown(self, app, monkeypatch):
        workspace = _workspace(_make_owner())
        workspace_id = workspace.id
        plugin_id = _plugin()
        dispatcher = self._dispatcher(monkeypatch)
        dispatcher.subscribe("project.created", lambda event: None, plugin_id=plugin_id)

        _dispatch(dispatcher, workspace_id=workspace_id)
        db.session.remove()

        assert len(_execution_rows(workspace_id)) == 1

    def test_timeout_records_timeout_outcome(self, app, monkeypatch):
        workspace = _workspace(_make_owner())
        plugin_id = _plugin()
        app.config["PLUGIN_EXECUTION_TIMEOUT"] = 0.05
        release = threading.Event()
        dispatcher = self._dispatcher(monkeypatch)
        dispatcher.subscribe("project.created", lambda event: release.wait(5), plugin_id=plugin_id)

        try:
            result = _dispatch(dispatcher, workspace_id=workspace.id)
        finally:
            release.set()
        db.session.commit()

        assert result["failed"] == 1
        row = _execution_rows(workspace.id)[0]
        assert row.event_metadata["outcome"] == "timeout"

    def test_error_records_error_outcome(self, app, monkeypatch):
        workspace = _workspace(_make_owner())
        plugin_id = _plugin()
        dispatcher = self._dispatcher(monkeypatch)

        def fail(event):
            raise ValueError("boom-payload")

        dispatcher.subscribe("project.created", fail, plugin_id=plugin_id)

        _dispatch(dispatcher, workspace_id=workspace.id)
        db.session.commit()

        row = _execution_rows(workspace.id)[0]
        assert row.event_metadata["outcome"] == "error"
        assert "boom-payload" not in str(row.event_metadata)

    def test_limit_exceeded_records_limit_outcome(self, app, monkeypatch):
        workspace = _workspace(_make_owner())
        plugin_id = _plugin()
        app.config["PLUGIN_MAX_OUTPUT_BYTES"] = 10
        dispatcher = self._dispatcher(monkeypatch)
        dispatcher.subscribe("project.created", lambda event: "x" * 100, plugin_id=plugin_id)

        _dispatch(dispatcher, workspace_id=workspace.id)
        db.session.commit()

        row = _execution_rows(workspace.id)[0]
        assert row.event_metadata["outcome"] == "limit_exceeded"


class TestExecutionLogEndpoint:
    def _url(self, workspace_id):
        return f"/plugins/api/workspaces/{workspace_id}/plugin-executions"

    def test_owner_reads_execution_log(self, client, make_user, login):
        owner = make_user()
        login()
        workspace = _workspace(owner)
        record_plugin_execution(
            workspace.id,
            "audit-plugin",
            version="1.0.0",
            capability="PROJECT_READ",
            outcome="success",
            duration_ms=1.25,
        )
        db.session.commit()

        response = client.get(self._url(workspace.id))

        assert response.status_code == 200
        payload = response.get_json()
        assert payload["total"] == 1
        item = payload["items"][0]
        assert item["plugin_id"] == "audit-plugin"
        assert item["version"] == "1.0.0"
        assert item["capability"] == "PROJECT_READ"
        assert item["outcome"] == "success"
        assert item["duration_ms"] == 1.25

    def test_member_gets_structured_403(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner)
        db.session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role="contributor")
        )
        db.session.commit()
        login(email="member@example.com")

        response = client.get(self._url(workspace.id))

        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"

    def test_non_member_gets_404(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        outsider = make_user(username="outsider", email="outsider@example.com")
        workspace = _workspace(owner)
        login(email=outsider.email)

        assert client.get(self._url(workspace.id)).status_code == 404

    def test_invalid_cursor_is_a_structured_400(self, client, make_user, login):
        owner = make_user()
        login()
        workspace = _workspace(owner)

        response = client.get(f"{self._url(workspace.id)}?cursor=not-base64")

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_pagination"

    def test_cursor_walks_pages_without_duplicates(self, client, make_user, login):
        owner = make_user()
        login()
        workspace = _workspace(owner)
        for index in range(5):
            record_plugin_execution(
                workspace.id,
                f"plugin-{index}",
                version="1.0.0",
                capability="PROJECT_READ",
                outcome="success",
                duration_ms=1,
            )
        db.session.commit()

        first = client.get(f"{self._url(workspace.id)}?limit=2").get_json()
        second = client.get(
            f"{self._url(workspace.id)}?limit=2&cursor={first['next_cursor']}"
        ).get_json()

        first_ids = {item["id"] for item in first["items"]}
        second_ids = {item["id"] for item in second["items"]}
        assert len(first_ids) == 2
        assert first_ids.isdisjoint(second_ids)
        assert first["next_cursor"]
        assert second["items"]

    def test_cursor_requires_the_expected_shape(self, client, make_user, login):
        owner = make_user()
        login()
        workspace = _workspace(owner)

        response = client.get(f"{self._url(workspace.id)}?cursor={encode_cursor({'x': 1})}")

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_pagination"


def _make_owner():
    from app.models import User

    user = User(username="audit-owner", email="audit-owner@example.com")
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user
