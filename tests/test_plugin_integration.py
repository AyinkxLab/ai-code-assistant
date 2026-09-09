"""Plugin end-to-end integration tests (#196).

Exercise the *real* plugin architecture rather than mocked internals:

1. a plugin manifest is written to a temp directory and parsed/validated by
   ``PluginManifest.from_file``;
2. the manifest is installed through the real management API
   (``POST /plugins/api/workspaces/<id>/plugins/install``), which creates the
   ``Plugin`` + ``PluginInstallation`` rows and stores the manifest-provided
   configuration — with **no** automatic capability grant;
3. a workspace context (owner + a real project) is created;
4. the required capability is granted **explicitly** (real grant API);
5. the plugin subscribes to ``project.created``;
6. a real request emits the event through ``emit_event``/``dispatch``;
7. the authorized handler runs and persists a result;
8. a failing handler is isolated so other handlers and the request still work;
9. a plugin *without* the grant is denied end-to-end (fail closed).

No external services and no monkeypatching of the plugin internals are used.
"""

import io
import json
import zipfile

import pytest

from app.extensions import db
from app.models import CapabilityGrant, PluginInstallation, Workspace
from app.services.capabilities import CapabilityStore
from app.services.events import get_dispatcher
from app.services.plugins import PluginManifest

CAPS = ["PROJECT_READ"]
CONFIG = {"endpoint": "https://hooks.example.invalid/repo-created"}


@pytest.fixture()
def workspace(app, make_user, login):
    user = make_user()
    login()
    ws = Workspace(user_id=user.id, name="Plugin integration workspace")
    db.session.add(ws)
    db.session.commit()
    return ws


def _manifest_dict(plugin_id):
    return {
        "id": plugin_id,
        "name": "Integration Observer",
        "version": "0.1.0",
        "description": "Observes project.created end to end",
        "author": "tester",
        "entry_point": "plugins.test:TestPlugin",
        "capabilities": CAPS,
        "configuration": CONFIG,
    }


def _write_manifest(tmp_path, plugin_id):
    manifest_file = tmp_path / "manifest.json"
    manifest_file.write_text(json.dumps(_manifest_dict(plugin_id)), encoding="utf-8")
    return PluginManifest.from_file(manifest_file)  # real parse + validation


def _install(client, workspace_id, manifest_dict):
    return client.post(
        f"/plugins/api/workspaces/{workspace_id}/plugins/install",
        json={"manifest": manifest_dict},
    )


def _grant(client, workspace_id, plugin_id, capability="PROJECT_READ"):
    return client.post(
        f"/plugins/api/workspaces/{workspace_id}/plugins/{plugin_id}/capabilities",
        json={"grant": [capability], "revoke": []},
    )


def _zip_bytes(entries):
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in entries:
            archive.writestr(name, data)
    return buffer.getvalue()


def _upload_project(client, workspace_id):
    return client.post(
        f"/workspaces/api/workspaces/{workspace_id}/projects",
        data={"file": (io.BytesIO(_zip_bytes([("main.py", "print('hi')\n")])), "demo.zip")},
        content_type="multipart/form-data",
    )


def _subscribe(dispatcher, event_type, plugin_id, handler):
    dispatcher.subscribe(event_type, handler, plugin_id=plugin_id)
    return handler


class TestInstallConfigureGrantExecute:
    def test_full_flow_executes_and_persists(self, app, client, workspace, tmp_path):
        manifest = _write_manifest(tmp_path, "e2e-observer")
        assert manifest.id == "e2e-observer"

        # Install through the real API.
        resp = _install(client, workspace.id, manifest.to_dict())
        assert resp.status_code == 201
        plugin_id = resp.get_json()["id"]

        # The manifest-provided configuration is stored on the installation.
        installation = PluginInstallation.query.filter_by(
            plugin_id=plugin_id, workspace_id=workspace.id
        ).one()
        assert installation.config == CONFIG

        # No capability is granted implicitly by installing.
        assert (
            CapabilityGrant.query.filter_by(plugin_id=plugin_id, workspace_id=workspace.id).count()
            == 0
        )

        # Explicitly grant the capability the event needs.
        grant_resp = _grant(client, workspace.id, plugin_id)
        assert grant_resp.status_code == 200
        assert grant_resp.get_json()["granted_capabilities"] == ["project:read"]
        assert CapabilityStore.has_capability(plugin_id, workspace.id, "project:read")

        # Subscribe a real handler and record results to a file.
        dispatcher = get_dispatcher()
        received = []
        result_file = tmp_path / "ran.txt"

        def handler(event):
            received.append(event)
            with open(result_file, "a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {
                            "event_type": event.event_type,
                            "workspace_id": event.workspace_id,
                            "project_id": event.data.get("project_id"),
                        }
                    )
                    + "\n"
                )

        _subscribe(dispatcher, "project.created", plugin_id, handler)
        try:
            resp = _upload_project(client, workspace.id)
        finally:
            dispatcher.unsubscribe("project.created", handler)

        # The real request ran the plugin handler and it persisted its result.
        assert resp.status_code == 201
        project_id = resp.get_json()["id"]
        assert len(received) == 1
        assert received[0].event_type == "project.created"
        assert received[0].workspace_id == workspace.id
        lines = result_file.read_text(encoding="utf-8").strip().splitlines()
        assert len(lines) == 1
        recorded = json.loads(lines[0])
        assert recorded["event_type"] == "project.created"
        assert recorded["workspace_id"] == workspace.id
        assert recorded["project_id"] == project_id

    def test_disabled_installation_never_executes(self, app, client, workspace):
        plugin_id = "e2e-disabled"
        install_resp = _install(client, workspace.id, _manifest_dict(plugin_id))
        assert install_resp.status_code == 201, install_resp.get_json()
        _grant(client, workspace.id, plugin_id)
        assert (
            client.post(
                f"/plugins/api/workspaces/{workspace.id}/plugins/{plugin_id}/disable"
            ).status_code
            == 200
        )

        dispatcher = get_dispatcher()
        received = []
        handler = _subscribe(
            dispatcher, "project.created", plugin_id, lambda event: received.append(event)
        )
        try:
            resp = _upload_project(client, workspace.id)
        finally:
            dispatcher.unsubscribe("project.created", handler)

        assert resp.status_code == 201
        assert received == []

    def test_ungranted_plugin_is_denied(self, app, client, workspace):
        # Installed but never granted the required capability.
        plugin_id = "e2e-ungranted"
        _install(client, workspace.id, _manifest_dict(plugin_id))

        dispatcher = get_dispatcher()
        received = []
        handler = _subscribe(
            dispatcher, "project.created", plugin_id, lambda event: received.append(event)
        )
        try:
            resp = _upload_project(client, workspace.id)
        finally:
            dispatcher.unsubscribe("project.created", handler)

        # The request still succeeds, but the un-granted plugin never runs.
        assert resp.status_code == 201
        assert received == []


class TestFailureIsolation:
    def test_failing_handler_isolated_and_others_still_run(self, app, client, workspace):
        good_id = "e2e-good"
        bad_id = "e2e-bad"
        for plugin_id in (good_id, bad_id):
            _install(client, workspace.id, _manifest_dict(plugin_id))
            _grant(client, workspace.id, plugin_id)

        dispatcher = get_dispatcher()
        ran = []

        def bad_handler(event):
            raise RuntimeError("plugin boom")

        def good_handler(event):
            ran.append(event)

        _subscribe(dispatcher, "project.created", bad_id, bad_handler)
        _subscribe(dispatcher, "project.created", good_id, good_handler)
        try:
            resp = _upload_project(client, workspace.id)
        finally:
            dispatcher.unsubscribe("project.created", bad_handler)
            dispatcher.unsubscribe("project.created", good_handler)

        # The failing plugin neither breaks the request nor stops the good one.
        assert resp.status_code == 201
        assert len(ran) == 1
        assert ran[0].event_type == "project.created"
