"""Tests for per-workspace plugin configuration (#166).

Covers owner-scoped config read/update endpoints, validation against the
manifest's declared ``configuration`` (unknown keys / wrong types -> 400),
authorization/isolation, and ``PluginInstallation`` persistence.
"""

import pytest

from app.extensions import db
from app.models import Plugin, PluginInstallation, Workspace, WorkspaceMember
from app.services.plugin_config import validate_plugin_config

DECLARED = {
    "enabled_networks": ["testnet"],
    "poll_seconds": 30,
    "webhook_url": "",
}


def _manifest(plugin_id="cfg-plugin"):
    return {
        "id": plugin_id,
        "name": "Config Plugin",
        "version": "0.1.0",
        "description": "test",
        "author": "tester",
        "entry_point": "plugins.test:TestPlugin",
        "capabilities": ["PROJECT_READ"],
        "configuration": DECLARED,
    }


def _install(client, workspace_id, plugin_id="cfg-plugin"):
    return client.post(
        f"/plugins/api/workspaces/{workspace_id}/plugins/install",
        json={"manifest": _manifest(plugin_id)},
    )


def _member(db_, ws, user, role="contributor"):
    membership = WorkspaceMember(workspace_id=ws.id, user_id=user.id, role=role)
    db.session.add(membership)
    db.session.commit()
    return membership


@pytest.fixture()
def workspace(app, make_user, login):
    user = make_user()
    login()
    ws = Workspace(user_id=user.id, name="Config workspace")
    db.session.add(ws)
    db.session.commit()
    return ws


class TestPluginConfigService:
    def test_valid_object(self):
        assert validate_plugin_config(DECLARED, {"enabled_networks": ["mainnet"]}) == (True, "")

    def test_number_types_are_interchangeable(self):
        ok, _ = validate_plugin_config(DECLARED, {"poll_seconds": 15.5})
        assert ok is True

    def test_unknown_key_rejected(self):
        ok, reason = validate_plugin_config(DECLARED, {"bogus": 1})
        assert ok is False
        assert "Unknown configuration key(s): bogus" in reason

    def test_wrong_type_rejected(self):
        ok, reason = validate_plugin_config(DECLARED, {"enabled_networks": "mainnet"})
        assert ok is False
        assert "must be list" in reason

    def test_non_object_rejected(self):
        assert validate_plugin_config(None, ["a"]) == (
            False,
            "Configuration must be a JSON object.",
        )

    def test_undeclared_plugin_accepts_bounded_object(self):
        assert validate_plugin_config({}, {"anything": {"nested": True}})[0] is True

    def test_installation_to_dict_exposes_config(self, app, db, make_user):
        owner = make_user()
        ws = Workspace(user_id=owner.id, name="Model workspace")
        db.session.add(ws)
        db.session.commit()
        plugin = Plugin(
            id="cfg-model",
            name="Config",
            version="0.1.0",
            description="d",
            author="a",
            entry_point="plugins.test:TestPlugin",
            capabilities=["PROJECT_READ"],
        )
        db.session.add(plugin)
        installation = PluginInstallation(
            plugin_id="cfg-model",
            workspace_id=ws.id,
            config={"enabled_networks": ["testnet"]},
        )
        db.session.add(installation)
        db.session.commit()
        assert installation.to_dict()["config"] == {"enabled_networks": ["testnet"]}


class TestPluginConfigApi:
    def _config_path(self, workspace_id, plugin_id="cfg-plugin"):
        return f"/plugins/api/workspaces/{workspace_id}/plugins/{plugin_id}/config"

    def test_read_initial_config_is_manifest_default(self, app, client, workspace):
        assert _install(client, workspace.id).status_code == 201
        resp = client.get(self._config_path(workspace.id))
        assert resp.status_code == 200
        assert resp.get_json()["config"] == DECLARED

    def test_update_then_read_persists(self, app, client, workspace):
        assert _install(client, workspace.id).status_code == 201
        new_config = {"enabled_networks": ["mainnet"], "poll_seconds": 60, "webhook_url": ""}
        put = client.put(self._config_path(workspace.id), json={"config": new_config})
        assert put.status_code == 200
        assert put.get_json()["config"] == new_config

        got = client.get(self._config_path(workspace.id)).get_json()["config"]
        assert got == new_config

        installation = PluginInstallation.query.filter_by(
            plugin_id="cfg-plugin", workspace_id=workspace.id
        ).one()
        assert installation.config == new_config

    def test_unknown_key_returns_400(self, app, client, workspace):
        assert _install(client, workspace.id).status_code == 201
        resp = client.put(
            self._config_path(workspace.id),
            json={"config": {"nope": True}},
        )
        assert resp.status_code == 400
        assert "Unknown configuration key" in resp.get_json()["error"]

    def test_wrong_type_returns_400(self, app, client, workspace):
        assert _install(client, workspace.id).status_code == 201
        resp = client.put(
            self._config_path(workspace.id),
            json={"config": {"enabled_networks": "mainnet"}},
        )
        assert resp.status_code == 400
        assert "must be list" in resp.get_json()["error"]

    def test_config_is_workspace_isolated(self, app, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        ws_a = Workspace(user_id=owner.id, name="A")
        db.session.add(ws_a)
        db.session.commit()
        ws_b = Workspace(user_id=owner.id, name="B")
        db.session.add(ws_b)
        db.session.commit()
        assert _install(client, ws_a.id, "cfg-plugin").status_code == 201
        assert _install(client, ws_b.id, "cfg-plugin").status_code == 201

        new_config = {"enabled_networks": ["mainnet"], "poll_seconds": 60, "webhook_url": ""}
        assert (
            client.put(self._config_path(ws_a.id), json={"config": new_config}).status_code == 200
        )
        assert client.get(self._config_path(ws_b.id)).get_json()["config"] == DECLARED

    def test_missing_installation_404(self, app, client, workspace):
        assert client.get(self._config_path(workspace.id)).status_code == 404

    def test_member_cannot_read_or_update(self, app, client, make_user, login, db):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        ws = Workspace(user_id=owner.id, name="Members workspace")
        db.session.add(ws)
        db.session.commit()
        login(email="owner@example.com")
        assert _install(client, ws.id).status_code == 201
        _member(db, ws, member, role="contributor")

        login(email="member@example.com")
        assert client.get(self._config_path(ws.id)).status_code == 403
        resp = client.put(self._config_path(ws.id), json={"config": {"enabled_networks": []}})
        assert resp.status_code == 403

    def test_outsider_gets_404(self, app, client, make_user, login, db):
        owner = make_user(username="owner", email="owner@example.com")
        make_user(username="other", email="other@example.com")
        ws = Workspace(user_id=owner.id, name="Hidden")
        db.session.add(ws)
        db.session.commit()
        login(email="owner@example.com")
        assert _install(client, ws.id).status_code == 201
        login(email="other@example.com")
        assert client.get(self._config_path(ws.id)).status_code == 404
