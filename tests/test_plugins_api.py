"""Tests for the plugin management API (Phase 8 #163).

Covers workspace-scoped list/inspect/install/enable/disable and explicit
capability grants, with authorization fail-closed, plugin identity binding,
workspace isolation, capability safety, and integration with the #164
dispatch-time enforcement.
"""

from app.extensions import db
from app.models import (
    CapabilityGrant,
    Plugin,
    PluginInstallation,
    Project,
    Workspace,
    WorkspaceMember,
)
from app.services.capabilities import CapabilityStore
from app.services.events import Event, get_dispatcher

VALID_MANIFEST = {
    "id": "test-plugin",
    "name": "Test Plugin",
    "version": "0.1.0",
    "description": "A test plugin",
    "author": "Tester",
    "entry_point": "plugins.test:TestPlugin",
    "capabilities": ["PROJECT_READ"],
}


def _user(db_, make_user, username, email):
    return make_user(username=username, email=email)


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


def _install(client, ws_id, manifest):
    return client.post(
        f"/plugins/api/workspaces/{ws_id}/plugins/install",
        json={"manifest": manifest},
    )


class TestPluginListing:
    def test_authorized_user_can_list_plugins(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        assert _install(client, ws.id, VALID_MANIFEST).status_code == 201

        resp = client.get(f"/plugins/api/workspaces/{ws.id}/plugins")
        assert resp.status_code == 200
        data = resp.get_json()
        plugins = {p["id"]: p for p in data["plugins"]}
        assert "test-plugin" in plugins
        assert plugins["test-plugin"]["installed"] is True
        assert plugins["test-plugin"]["enabled"] is True
        assert plugins["test-plugin"]["declared_capabilities"] == ["PROJECT_READ"]

    def test_list_requires_membership(self, app, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        make_user(username="outsider", email="outsider@example.com")
        ws = _workspace(db, owner)
        login(email="outsider@example.com")
        assert client.get(f"/plugins/api/workspaces/{ws.id}/plugins").status_code == 404

    def test_member_can_view_but_not_manage(self, app, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        ws = _workspace(db, owner)
        _member(db, ws, member, role="contributor")
        login(email="member@example.com")

        assert client.get(f"/plugins/api/workspaces/{ws.id}/plugins").status_code == 200
        assert _install(client, ws.id, VALID_MANIFEST).status_code == 403
        path = f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/enable"
        assert client.post(path).status_code == 403


class TestPluginInspection:
    def test_authorized_user_can_inspect(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        _install(client, ws.id, VALID_MANIFEST)
        resp = client.get(f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin")
        assert resp.status_code == 200
        data = resp.get_json()
        assert data["id"] == "test-plugin"
        assert data["version"] == "0.1.0"
        assert data["author"] == "Tester"

    def test_cannot_inspect_another_workspace(self, app, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        make_user(username="outsider", email="outsider@example.com")
        ws = _workspace(db, owner)
        _install(client, ws.id, VALID_MANIFEST)
        login(email="outsider@example.com")
        assert client.get(f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin").status_code == 404

    def test_inspect_unknown_plugin_404(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        assert client.get(f"/plugins/api/workspaces/{ws.id}/plugins/ghost").status_code == 404


class TestInstallation:
    def test_valid_manifest_installed(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        resp = _install(client, ws.id, VALID_MANIFEST)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["installed"] is True

        plugin = db.session.get(Plugin, "test-plugin")
        assert plugin is not None
        assert plugin.id == "test-plugin"
        installation = PluginInstallation.query.filter_by(
            plugin_id="test-plugin", workspace_id=ws.id
        ).first()
        assert installation is not None
        assert installation.enabled is True

    def test_installation_bound_to_manifest_id(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        resp = _install(client, ws.id, VALID_MANIFEST)
        assert resp.status_code == 201
        # The client could not redirect the id: the manifest id is authoritative.
        data = resp.get_json()
        assert data["id"] == "test-plugin"
        assert db.session.get(Plugin, "test-plugin").id == "test-plugin"

    def test_manifest_cannot_impersonate_existing_plugin(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        assert _install(client, ws.id, VALID_MANIFEST).status_code == 201

        # A different manifest claiming the same id with a different entry point
        # must be rejected, not silently registered as a second definition.
        forged = dict(VALID_MANIFEST, entry_point="plugins.evil:EvilPlugin")
        resp = _install(client, ws.id, forged)
        assert resp.status_code == 409
        assert db.session.get(Plugin, "test-plugin").entry_point == "plugins.test:TestPlugin"

    def test_invalid_manifest_rejected(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        bad = dict(VALID_MANIFEST)
        del bad["version"]
        resp = _install(client, ws.id, bad)
        assert resp.status_code == 400
        assert "Invalid plugin manifest" in resp.get_json()["error"]
        assert db.session.get(Plugin, "test-plugin") is None

    def test_invalid_capability_rejected(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        bad = dict(VALID_MANIFEST, capabilities=["NOT_A_CAPABILITY"])
        resp = _install(client, ws.id, bad)
        assert resp.status_code == 400

    def test_manifest_required(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        resp = client.post(
            f"/plugins/api/workspaces/{ws.id}/plugins/install",
            json={},
        )
        assert resp.status_code == 400

    def test_duplicate_installation_409(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        assert _install(client, ws.id, VALID_MANIFEST).status_code == 201
        resp = _install(client, ws.id, VALID_MANIFEST)
        assert resp.status_code == 409
        assert (
            db.session.query(PluginInstallation)
            .filter_by(plugin_id="test-plugin", workspace_id=ws.id)
            .count()
            == 1
        )

    def test_installation_belongs_to_correct_workspace(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws_a = _workspace(db, owner, "A")
        ws_b = _workspace(db, owner, "B")
        assert _install(client, ws_a.id, VALID_MANIFEST).status_code == 201

        data_b = client.get(f"/plugins/api/workspaces/{ws_b.id}/plugins").get_json()
        plugin_b = next(p for p in data_b["plugins"] if p["id"] == "test-plugin")
        assert plugin_b["installed"] is False

        data_a = client.get(f"/plugins/api/workspaces/{ws_a.id}/plugins").get_json()
        plugin_a = next(p for p in data_a["plugins"] if p["id"] == "test-plugin")
        assert plugin_a["installed"] is True

    def test_cross_workspace_install_rejected(self, app, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        other = make_user(username="other", email="other@example.com")
        ws_b = _workspace(db, other, "B")
        login(email="owner@example.com")
        # Owner of A cannot install into B (not a member of B) -> 404 fail closed.
        assert _install(client, ws_b.id, VALID_MANIFEST).status_code == 404

    def test_no_auto_grant_on_install(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        assert _install(client, ws.id, VALID_MANIFEST).status_code == 201
        grants = CapabilityGrant.query.filter_by(plugin_id="test-plugin", workspace_id=ws.id).all()
        assert len(grants) == 0
        data = client.get(f"/plugins/api/workspaces/{ws.id}/plugins").get_json()
        plugin = next(p for p in data["plugins"] if p["id"] == "test-plugin")
        assert plugin["granted_capabilities"] == []


class TestEnableDisable:
    def test_disable_and_enable(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        _install(client, ws.id, VALID_MANIFEST)

        resp = client.post(f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/disable")
        assert resp.status_code == 200
        assert resp.get_json()["enabled"] is False

        resp = client.post(f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/enable")
        assert resp.status_code == 200
        assert resp.get_json()["enabled"] is True

    def test_enable_does_not_auto_grant(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        _install(client, ws.id, VALID_MANIFEST)
        client.post(f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/enable")
        grants = CapabilityGrant.query.filter_by(plugin_id="test-plugin", workspace_id=ws.id).all()
        assert len(grants) == 0

    def test_enable_disable_requires_owner(self, app, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        ws = _workspace(db, owner)
        _member(db, ws, member, role="viewer")
        login(email="member@example.com")
        assert (
            client.post(f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/disable").status_code
            == 403
        )

    def test_enable_unknown_plugin_404(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        path = f"/plugins/api/workspaces/{ws.id}/plugins/ghost/enable"
        assert client.post(path).status_code == 404

    def test_disabled_plugin_does_not_execute(self, app, client, make_user, login):
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
        _install(client, ws.id, VALID_MANIFEST)
        CapabilityStore.grant("test-plugin", ws.id, "project:read")

        dispatcher = get_dispatcher()
        received = []

        def handler(event):
            received.append(event)

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
            assert len(received) == 0
            assert result["denied"] == 1

            client.post(f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/enable")
            result = dispatcher.dispatch(event)
            assert len(received) == 1
            assert result["denied"] == 0
        finally:
            dispatcher.unsubscribe("project.created", handler)


class TestCapabilityGrants:
    def test_grant_and_revoke(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        _install(client, ws.id, VALID_MANIFEST)

        resp = client.post(
            f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/capabilities",
            json={"grant": ["PROJECT_READ"], "revoke": []},
        )
        assert resp.status_code == 200
        assert resp.get_json()["granted_capabilities"] == ["project:read"]

        resp = client.post(
            f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/capabilities",
            json={"grant": [], "revoke": ["PROJECT_READ"]},
        )
        assert resp.status_code == 200
        assert resp.get_json()["granted_capabilities"] == []

    def test_grant_requires_owner(self, app, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        ws = _workspace(db, owner)
        _member(db, ws, member, role="contributor")
        login(email="member@example.com")
        resp = client.post(
            f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/capabilities",
            json={"grant": ["PROJECT_READ"]},
        )
        assert resp.status_code == 403

    def test_grant_undeclared_capability_rejected(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        _install(client, ws.id, VALID_MANIFEST)
        resp = client.post(
            f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/capabilities",
            json={"grant": ["STELLAR_WRITE"]},
        )
        assert resp.status_code == 400
        assert "not declared" in resp.get_json()["error"]

    def test_capability_info_workspace_isolated(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws_a = _workspace(db, owner, "A")
        ws_b = _workspace(db, owner, "B")
        _install(client, ws_a.id, VALID_MANIFEST)
        _install(client, ws_b.id, VALID_MANIFEST)
        client.post(
            f"/plugins/api/workspaces/{ws_a.id}/plugins/test-plugin/capabilities",
            json={"grant": ["PROJECT_READ"]},
        )

        data_a = client.get(f"/plugins/api/workspaces/{ws_a.id}/plugins").get_json()
        data_b = client.get(f"/plugins/api/workspaces/{ws_b.id}/plugins").get_json()
        plugin_a = next(p for p in data_a["plugins"] if p["id"] == "test-plugin")
        plugin_b = next(p for p in data_b["plugins"] if p["id"] == "test-plugin")
        assert plugin_a["granted_capabilities"] == ["project:read"]
        assert plugin_b["granted_capabilities"] == []

    def test_grant_requires_installation(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        resp = client.post(
            f"/plugins/api/workspaces/{ws.id}/plugins/test-plugin/capabilities",
            json={"grant": ["PROJECT_READ"]},
        )
        assert resp.status_code == 404


class TestWorkspaceTampering:
    def test_client_supplied_workspace_cannot_bypass(self, app, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        other = make_user(username="other", email="other@example.com")
        ws = _workspace(db, other, "Other")
        login(email="owner@example.com")
        # Owner cannot read or manage another user's workspace by id.
        assert client.get(f"/plugins/api/workspaces/{ws.id}/plugins").status_code == 404
        assert _install(client, ws.id, VALID_MANIFEST).status_code == 404

    def test_list_workspaces_only_accessible(self, app, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        other = make_user(username="other", email="other@example.com")
        ws_own = _workspace(db, owner, "Own")
        _workspace(db, other, "Hidden")
        login(email="owner@example.com")
        data = client.get("/plugins/api/workspaces").get_json()
        ids = [w["id"] for w in data]
        assert ws_own.id in ids
        assert all(w["role"] == "owner" for w in data)


class TestErrorHandling:
    def test_errors_do_not_leak_internals(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        resp = _install(client, ws.id, {"id": "x"})  # missing required fields
        assert resp.status_code == 400
        body = resp.get_json()["error"]
        assert "Traceback" not in body
        assert body.startswith("Invalid plugin manifest:")

        dup = _install(client, ws.id, {"id": "x"})
        # Still a clean validation error, not a server trace.
        assert dup.status_code == 400


class TestIdentityBindingNoSubscriptions:
    def test_install_creates_no_subscriptions(self, app, client, make_user, login):
        owner = make_user()
        login()
        ws = _workspace(db, owner)
        dispatcher = get_dispatcher()
        before = dispatcher.get_subscription_count()
        _install(client, ws.id, VALID_MANIFEST)
        after = dispatcher.get_subscription_count()
        # Installing a plugin must never register a trusted/internal handler.
        assert after == before
        assert "test-plugin" not in dispatcher.list_subscribers("project.created")
