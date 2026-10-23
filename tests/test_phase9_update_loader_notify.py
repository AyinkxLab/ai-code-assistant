"""Tests for #403, #406, #401 and #404.

- #406 controlled entry-point loader (registered-only, isolated failures).
- #401 atomic catalog update (single transaction, prior version on failure).
- #404 update notifications (created once per version, deduped, opt-out).
- #403 update/rollback UI and API wiring.
"""

from __future__ import annotations

import json
import sys

import pytest

from app.extensions import db
from app.models import (
    Notification,
    NotificationPreference,
    Plugin,
    PluginInstallation,
    PluginLockEntry,
    Workspace,
)
from app.services.plugin_catalog import parse_catalog
from app.services.plugin_ops import (
    PluginEntryPointMismatchError,
    atomic_update_from_catalog,
    rollback_from_catalog,
)
from app.services.plugin_signatures import InvalidPublisherSignatureError
from app.services.plugin_updates import notify_available_updates
from app.services.plugins import (
    Plugin as RuntimePlugin,
)
from app.services.plugins import (
    PluginEntryPointLoader,
    PluginLoadError,
    PluginManifest,
    PluginRegistry,
)


def _manifest(
    plugin_id="update-plugin",
    version="0.1.0",
    entry_point="plugins.up:UpPlugin",
    **extra,
):
    data = {
        "id": plugin_id,
        "name": "Update Plugin",
        "version": version,
        "description": "test",
        "author": "tester",
        "entry_point": entry_point,
        "capabilities": ["PROJECT_READ"],
    }
    data.update(extra)
    return data


def _workspace(owner, name="WS"):
    ws = Workspace(user_id=owner.id, name=name)
    db.session.add(ws)
    db.session.commit()
    return ws


def _plugin(plugin_id="update-plugin", version="0.1.0", entry_point="plugins.up:UpPlugin"):
    row = Plugin(
        id=plugin_id,
        name="Update Plugin",
        version=version,
        description="test",
        author="tester",
        entry_point=entry_point,
        capabilities=["PROJECT_READ"],
    )
    db.session.add(row)
    db.session.commit()
    return row


def _install(plugin, workspace):
    row = PluginInstallation(
        plugin_id=plugin.id,
        workspace_id=workspace.id,
        enabled=True,
        config={},
    )
    db.session.add(row)
    db.session.commit()
    return row


def _lock(plugin, workspace, owner, version=None):
    row = PluginLockEntry(
        workspace_id=workspace.id,
        owner_id=owner.id,
        plugin_id=plugin.id,
        version=version or plugin.version,
        source="https://plugins.example.com/p.zip",
        integrity="sha256:" + "a" * 64,
        dependencies=[],
    )
    db.session.add(row)
    db.session.commit()
    return row


def _catalog(entries):
    return parse_catalog({"version": 1, "plugins": list(entries)})


def _entry(plugin_id="update-plugin", version="0.2.0", **overrides):
    manifest = _manifest(plugin_id=plugin_id, version=version)
    entry = {
        "id": plugin_id,
        "version": version,
        "source": f"https://plugins.example.com/{plugin_id}-{version}.zip",
        "integrity": "a" * 64,
        "manifest": manifest,
    }
    entry.update(overrides)
    return entry


def _policy_config(*, policy="if-present", trusted=None):
    return {
        "PLUGIN_TRUST_POLICY": policy,
        "PLUGIN_TRUSTED_KEYS": trusted or {},
    }


# --------------------------------------------------------------------------
# #406 controlled entry-point loader
# --------------------------------------------------------------------------


@pytest.fixture()
def loadable_package(tmp_path, monkeypatch):
    package = tmp_path / "loader_pkg"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "mod.py").write_text(
        "class LoadedPlugin:\n"
        "    def __init__(self, app=None, manifest=None):\n"
        "        self.app = app\n"
        "        self.manifest = manifest\n",
        encoding="utf-8",
    )
    monkeypatch.syspath_prepend(str(tmp_path))
    try:
        yield "loader_pkg.mod:LoadedPlugin"
    finally:
        sys.modules.pop("loader_pkg.mod", None)
        sys.modules.pop("loader_pkg", None)


class TestControlledLoader:
    def test_loads_registered_entry_point(self, loadable_package):
        manifest = PluginManifest.from_dict(_manifest(entry_point=loadable_package))
        registry = PluginRegistry()
        plugin = RuntimePlugin(manifest)
        registry.register(plugin)

        loaded = registry.load_plugin("update-plugin")

        assert loaded.module is not None
        assert loaded.get_instance().__class__.__name__ == "LoadedPlugin"

    def test_unregistered_plugin_is_refused(self, loadable_package):
        manifest = PluginManifest.from_dict(_manifest(entry_point=loadable_package))
        registry = PluginRegistry()

        with pytest.raises(PluginLoadError, match="not registered"):
            registry.load_plugin("update-plugin")

        # Constructing the loader against a registry also refuses.
        with pytest.raises(PluginLoadError, match="not registered"):
            PluginEntryPointLoader(manifest, registry=registry)

    def test_missing_class_is_isolated(self, loadable_package):
        manifest = PluginManifest.from_dict(_manifest(entry_point="loader_pkg.mod:DoesNotExist"))
        with pytest.raises(PluginLoadError, match="not found"):
            RuntimePlugin(manifest).load()

    def test_import_failure_is_isolated(self, tmp_path, monkeypatch):
        package = tmp_path / "broken_pkg"
        package.mkdir()
        (package / "__init__.py").write_text("", encoding="utf-8")
        (package / "mod.py").write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        monkeypatch.syspath_prepend(str(tmp_path))

        manifest = PluginManifest.from_dict(_manifest(entry_point="broken_pkg.mod:Broken"))
        with pytest.raises(PluginLoadError, match="Cannot import"):
            RuntimePlugin(manifest).load()
        sys.modules.pop("broken_pkg.mod", None)
        sys.modules.pop("broken_pkg", None)

    def test_denied_ambient_module_is_refused(self):
        manifest = PluginManifest.from_dict(_manifest(entry_point="subprocess:run"))
        with pytest.raises(PluginLoadError, match="denied module"):
            PluginEntryPointLoader(manifest)


# --------------------------------------------------------------------------
# #401 atomic update
# --------------------------------------------------------------------------


class TestAtomicUpdate:
    def test_success_updates_plugin_and_lock_together(self, db, make_user):
        owner = make_user()
        ws = _workspace(owner)
        plugin = _plugin(version="0.1.0")
        lock = _lock(plugin, ws, owner, version="0.1.0")

        updated, entry = atomic_update_from_catalog(
            plugin.id,
            "0.2.0",
            workspace_id=ws.id,
            config=_policy_config(),
            catalog=_catalog([_entry()]),
        )

        assert updated.version == "0.2.0"
        assert entry.version == "0.2.0"
        refreshed = db.session.get(PluginLockEntry, lock.id)
        assert refreshed.version == "0.2.0"
        assert refreshed.integrity == "sha256:" + "a" * 64

    def test_entry_point_mismatch_leaves_prior_version(self, db, make_user):
        owner = make_user()
        ws = _workspace(owner)
        plugin = _plugin(version="0.1.0")
        lock = _lock(plugin, ws, owner, version="0.1.0")
        bad = _entry(version="0.2.0")
        bad["manifest"] = _manifest(version="0.2.0", entry_point="plugins.other:Other")

        with pytest.raises(PluginEntryPointMismatchError):
            atomic_update_from_catalog(
                plugin.id,
                "0.2.0",
                workspace_id=ws.id,
                config=_policy_config(),
                catalog=_catalog([bad]),
            )

        db.session.expire_all()
        assert db.session.get(Plugin, plugin.id).version == "0.1.0"
        assert db.session.get(PluginLockEntry, lock.id).version == "0.1.0"

    def test_transaction_failure_rolls_back_everything(self, db, make_user, monkeypatch):
        owner = make_user()
        ws = _workspace(owner)
        plugin = _plugin(version="0.1.0")
        lock = _lock(plugin, ws, owner, version="0.1.0")

        def boom(*_args, **_kwargs):
            raise RuntimeError("lock write failed")

        monkeypatch.setattr("app.services.plugin_ops._upsert_lock_entry", boom)

        with pytest.raises(RuntimeError):
            atomic_update_from_catalog(
                plugin.id,
                "0.2.0",
                workspace_id=ws.id,
                config=_policy_config(),
                catalog=_catalog([_entry()]),
            )

        db.session.expire_all()
        assert db.session.get(Plugin, plugin.id).version == "0.1.0"
        assert db.session.get(PluginLockEntry, lock.id).version == "0.1.0"

    def test_trust_policy_blocks_update_before_writes(self, db, make_user):
        owner = make_user()
        ws = _workspace(owner)
        plugin = _plugin(version="0.1.0")
        _lock(plugin, ws, owner, version="0.1.0")
        entry = _entry(signature={"publisher": "acme", "algorithm": "ed25519", "value": "AAAA"})

        with pytest.raises(InvalidPublisherSignatureError):
            atomic_update_from_catalog(
                plugin.id,
                "0.2.0",
                workspace_id=ws.id,
                config=_policy_config(policy="required"),
                catalog=_catalog([entry]),
            )

        db.session.expire_all()
        assert db.session.get(Plugin, plugin.id).version == "0.1.0"

    def test_catalog_rollback_uses_previous_version(self, db, make_user):
        owner = make_user()
        ws = _workspace(owner)
        plugin = _plugin(version="0.2.0")
        _lock(plugin, ws, owner, version="0.2.0")
        rolled_back, entry = rollback_from_catalog(
            plugin.id,
            workspace_id=ws.id,
            config=_policy_config(),
            catalog=_catalog([_entry(version="0.1.0"), _entry(version="0.2.0")]),
        )

        assert rolled_back.version == "0.1.0"
        assert entry.version == "0.1.0"


# --------------------------------------------------------------------------
# #404 update notifications
# --------------------------------------------------------------------------


class TestUpdateNotifications:
    def _setup(self, make_user):
        owner = make_user()
        ws = _workspace(owner)
        plugin = _plugin(version="0.1.0")
        _install(plugin, ws)
        return owner, ws, plugin

    def test_notifies_owner_once_per_version(self, db, make_user):
        owner, ws, _plugin_row = self._setup(make_user)
        catalog = _catalog([_entry(version="0.2.0")])

        first = notify_available_updates(ws, catalog=catalog)
        second = notify_available_updates(ws, catalog=catalog)

        assert len(first) == 1
        assert second == []
        notification = Notification.query.filter_by(user_id=owner.id, type="plugin_update").one()
        assert notification.payload["plugin_id"] == "update-plugin"
        assert notification.payload["version"] == "0.2.0"
        assert notification.workspace_id == ws.id

    def test_new_version_notifies_again(self, db, make_user):
        _owner, ws, _plugin_row = self._setup(make_user)

        notify_available_updates(ws, catalog=_catalog([_entry(version="0.2.0")]))
        second = notify_available_updates(
            ws, catalog=_catalog([_entry(version="0.2.0"), _entry(version="0.3.0")])
        )

        assert len(second) == 1
        assert second[0].payload["version"] == "0.3.0"

    def test_respects_opt_out_preference(self, db, make_user):
        owner, ws, _plugin_row = self._setup(make_user)
        pref = NotificationPreference(user_id=owner.id, ai_events=False)
        db.session.add(pref)
        db.session.commit()

        created = notify_available_updates(ws, catalog=_catalog([_entry(version="0.2.0")]))

        assert created == []
        assert Notification.query.filter_by(type="plugin_update").count() == 0

    def test_incompatible_update_is_not_notified(self, db, make_user):
        _owner, ws, _plugin_row = self._setup(make_user)
        entry = _entry(version="0.2.0")
        entry["manifest"] = _manifest(version="0.2.0", compatibility=">=99.0.0")

        created = notify_available_updates(ws, catalog=_catalog([entry]))

        assert created == []


# --------------------------------------------------------------------------
# #400 update availability check API
# --------------------------------------------------------------------------


class TestUpdateAvailabilityApi:
    def _setup(self, app, client, make_user, login, entries):
        owner = make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        ws = _workspace(owner)
        plugin = _plugin(version="0.1.0")
        _install(plugin, ws)
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(app, entries))
        return ws, plugin

    @staticmethod
    def _url(ws):
        return f"/plugins/api/workspaces/{ws.id}/plugins/updates"

    def test_reports_only_compatible_newer_versions(self, app, client, make_user, login):
        ws, plugin = self._setup(
            app,
            client,
            make_user,
            login,
            [
                _entry(version="0.2.0"),
                _entry(version="0.3.0"),
                {
                    **_entry(version="0.4.0"),
                    "manifest": _manifest(version="0.4.0", compatibility=">=99.0.0"),
                },
            ],
        )

        response = client.get(self._url(ws))

        assert response.status_code == 200
        updates = response.get_json()["updates"]
        assert len(updates) == 1
        assert updates[0]["plugin_id"] == plugin.id
        assert updates[0]["current_version"] == "0.1.0"
        assert updates[0]["available_version"] == "0.3.0"
        assert updates[0]["compatible"] is True

    def test_reports_nothing_when_catalog_is_not_newer(self, app, client, make_user, login):
        ws, _plugin_row = self._setup(
            app,
            client,
            make_user,
            login,
            [_entry(version="0.1.0"), _entry(version="0.0.9")],
        )

        response = client.get(self._url(ws))

        assert response.status_code == 200
        assert response.get_json()["updates"] == []

    def test_reports_nothing_when_the_only_update_is_incompatible(
        self, app, client, make_user, login
    ):
        ws, _plugin_row = self._setup(
            app,
            client,
            make_user,
            login,
            [
                {
                    **_entry(version="0.2.0"),
                    "manifest": _manifest(version="0.2.0", compatibility=">=99.0.0"),
                }
            ],
        )

        response = client.get(self._url(ws))

        assert response.status_code == 200
        assert response.get_json()["updates"] == []

    def test_member_without_workspace_access_gets_404(self, app, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        ws = _workspace(owner)
        make_user(username="stranger", email="stranger@example.com")
        login(email="stranger@example.com")
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(app, [_entry(version="0.2.0")]))

        response = client.get(self._url(ws))

        assert response.status_code == 404


# --------------------------------------------------------------------------
# #403 update/rollback UI + API
# --------------------------------------------------------------------------


class TestUpdateRollbackSurface:
    def _setup(self, make_user, login, username="owner", email="owner@example.com"):
        owner = make_user(username=username, email=email)
        login(email=email)
        ws = _workspace(owner)
        plugin = _plugin(version="0.1.0")
        _install(plugin, ws)
        _lock(plugin, ws, owner, version="0.1.0")
        return owner, ws, plugin

    def _detail_url(self, ws, plugin):
        return f"/plugins/workspaces/{ws.id}/plugins/{plugin.id}"

    def test_detail_shows_update_and_rollback_controls(self, app, client, make_user, login):
        _owner, ws, plugin = self._setup(make_user, login)
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(app, [_entry(version="0.2.0")]))
        app.config["PLUGIN_TRUST_POLICY"] = "if-present"
        app.config["PLUGIN_TRUSTED_KEYS"] = {}

        # The lock entry is current, so rollback comes from the catalog target.
        response = client.get(self._detail_url(ws, plugin))
        html = response.get_data(as_text=True)

        assert response.status_code == 200
        assert "Update available" in html
        assert 'data-version="0.2.0"' in html
        assert "plugin-detail-rollback" not in html  # no older version exists yet

    def test_detail_shows_rollback_when_older_lock_exists(self, app, client, make_user, login):
        _owner, ws, plugin = self._setup(make_user, login)
        plugin.version = "0.2.0"
        db.session.commit()
        app.config["PLUGIN_CATALOG_FILE"] = ""

        html = client.get(self._detail_url(ws, plugin)).get_data(as_text=True)

        assert "plugin-detail-rollback" in html
        assert "Rollback to v0.1.0" in html

    def test_update_plan_api(self, app, client, make_user, login):
        _owner, ws, plugin = self._setup(make_user, login)
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(app, [_entry(version="0.2.0")]))

        payload = client.get(
            f"/plugins/api/workspaces/{ws.id}/plugins/{plugin.id}/update-plan"
        ).get_json()

        assert payload["current_version"] == "0.1.0"
        assert payload["available_update"]["version"] == "0.2.0"
        assert payload["available_update"]["compatible"] is True

    def test_update_from_catalog_owner(self, app, client, make_user, login):
        _owner, ws, plugin = self._setup(make_user, login)
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(app, [_entry(version="0.2.0")]))
        app.config["PLUGIN_TRUST_POLICY"] = "if-present"
        app.config["PLUGIN_TRUSTED_KEYS"] = {}

        response = client.post(
            f"/plugins/api/workspaces/{ws.id}/plugins/{plugin.id}/update-from-catalog",
            json={"version": "0.2.0"},
        )

        assert response.status_code == 200
        assert response.get_json()["version"] == "0.2.0"
        assert db.session.get(Plugin, plugin.id).version == "0.2.0"

    def test_update_from_catalog_member_forbidden(self, app, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        ws = _workspace(owner)
        plugin = _plugin(version="0.1.0")
        _install(plugin, ws)
        from app.models import WorkspaceMember

        db.session.add(WorkspaceMember(workspace_id=ws.id, user_id=member.id, role="contributor"))
        db.session.commit()
        login(email="member@example.com")
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(app, [_entry(version="0.2.0")]))

        response = client.post(
            f"/plugins/api/workspaces/{ws.id}/plugins/{plugin.id}/update-from-catalog",
            json={"version": "0.2.0"},
        )

        assert response.status_code == 403

    def test_check_updates_endpoint(self, app, client, make_user, login):
        _owner, ws, _plugin = self._setup(make_user, login)
        app.config["PLUGIN_CATALOG_FILE"] = str(_write_catalog(app, [_entry(version="0.2.0")]))

        response = client.post(f"/plugins/api/workspaces/{ws.id}/plugins/check-updates")

        assert response.status_code == 200
        assert response.get_json()["created"] == 1

    def test_catalog_rollback_endpoint(self, app, client, make_user, login):
        _owner, ws, plugin = self._setup(make_user, login)
        plugin.version = "0.2.0"
        db.session.commit()
        app.config["PLUGIN_CATALOG_FILE"] = str(
            _write_catalog(app, [_entry(version="0.1.0"), _entry(version="0.2.0")])
        )
        app.config["PLUGIN_TRUST_POLICY"] = "if-present"
        app.config["PLUGIN_TRUSTED_KEYS"] = {}

        response = client.post(f"/plugins/api/workspaces/{ws.id}/plugins/{plugin.id}/rollback")

        assert response.status_code == 200
        assert response.get_json()["version"] == "0.1.0"


def _write_catalog(app, entries):
    import tempfile
    from pathlib import Path

    directory = Path(tempfile.mkdtemp())
    path = directory / "catalog.json"
    path.write_text(json.dumps({"version": 1, "plugins": list(entries)}), encoding="utf-8")
    return path
