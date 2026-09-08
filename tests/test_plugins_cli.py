"""Tests for the operator-facing plugin CLI (#171).

Uses ``app.test_cli_runner()`` inside the Flask app context and asserts
exit codes (0 success, 1 not found, 2 validation/other), human-readable
output, and parseable ``--json`` output.
"""

import json

from app.extensions import db
from app.models import Plugin

VALID_MANIFEST = {
    "id": "cli-plugin",
    "name": "CLI Plugin",
    "version": "0.1.0",
    "description": "test plugin",
    "author": "tester",
    "entry_point": "plugins.test:TestPlugin",
    "capabilities": ["PROJECT_READ"],
    "permissions": ["read:project:files"],
    "dependencies": ["dep>=1.0"],
    "configuration": {"enabled_networks": ["testnet"]},
}


def _runner(app):
    return app.test_cli_runner()


def _add_plugin(plugin_id="cli-plugin", enabled=True):
    plugin = Plugin(
        id=plugin_id,
        name="CLI Plugin",
        version="0.1.0",
        description="test plugin",
        author="tester",
        entry_point="plugins.test:TestPlugin",
        capabilities=["PROJECT_READ"],
        permissions=["read:project:files"],
        dependencies=["dep>=1.0"],
        configuration={"enabled_networks": ["testnet"]},
        enabled=enabled,
    )
    db.session.add(plugin)
    db.session.commit()
    return plugin


def _write_local_manifest(tmp_path, data=None):
    directory = tmp_path / "plugin"
    directory.mkdir()
    manifest_file = directory / "manifest.json"
    manifest_file.write_text(json.dumps(data or VALID_MANIFEST), encoding="utf-8")
    return str(directory)


class TestListCommand:
    def test_list_empty(self, app):
        result = _runner(app).invoke(args=["plugins", "list"])
        assert result.exit_code == 0
        assert "0 plugin(s) registered." in result.output

    def test_list_shows_state(self, app):
        _add_plugin("enabled-one", enabled=True)
        _add_plugin("disabled-one", enabled=False)
        result = _runner(app).invoke(args=["plugins", "list"])
        assert result.exit_code == 0
        assert "enabled-one" in result.output
        assert "disabled-one" in result.output
        assert "enabled" in result.output
        assert "disabled" in result.output

    def test_list_json(self, app):
        _add_plugin("cli-plugin")
        result = _runner(app).invoke(args=["plugins", "list", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert any(p["id"] == "cli-plugin" and p["enabled"] is True for p in data)
        assert any("entry_point" in p for p in data)


class TestInspectCommand:
    def test_inspect_human(self, app):
        _add_plugin()
        result = _runner(app).invoke(args=["plugins", "inspect", "cli-plugin"])
        assert result.exit_code == 0
        assert "id: cli-plugin" in result.output
        assert "version: 0.1.0" in result.output
        assert "enabled: True" in result.output
        assert "PROJECT_READ" in result.output
        assert "testnet" in result.output

    def test_inspect_json_complete(self, app):
        _add_plugin()
        result = _runner(app).invoke(args=["plugins", "inspect", "cli-plugin", "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == "cli-plugin"
        assert data["enabled"] is True
        assert data["capabilities"] == ["PROJECT_READ"]
        assert data["permissions"] == ["read:project:files"]
        assert data["dependencies"] == ["dep>=1.0"]
        assert data["configuration"] == {"enabled_networks": ["testnet"]}

    def test_inspect_unknown_plugin(self, app):
        result = _runner(app).invoke(args=["plugins", "inspect", "ghost"])
        assert result.exit_code == 1
        assert "Plugin not found: ghost" in result.output

    def test_inspect_unknown_plugin_json(self, app):
        result = _runner(app).invoke(args=["plugins", "inspect", "ghost", "--json"])
        assert result.exit_code == 1
        assert json.loads(result.output)["error"] == "Plugin not found: ghost"


class TestEnableDisableCommands:
    def test_disable_and_enable_reflects_state(self, app):
        _add_plugin("cli-plugin", enabled=True)
        assert _runner(app).invoke(args=["plugins", "disable", "cli-plugin"]).exit_code == 0
        assert db.session.get(Plugin, "cli-plugin").enabled is False
        assert _runner(app).invoke(args=["plugins", "enable", "cli-plugin"]).exit_code == 0
        assert db.session.get(Plugin, "cli-plugin").enabled is True

    def test_enable_unknown(self, app):
        result = _runner(app).invoke(args=["plugins", "enable", "ghost"])
        assert result.exit_code == 1
        assert "Plugin not found: ghost" in result.output

    def test_json_state(self, app):
        _add_plugin("cli-plugin")
        result = _runner(app).invoke(args=["plugins", "disable", "cli-plugin", "--json"])
        assert result.exit_code == 0
        assert json.loads(result.output) == {"enabled": False, "id": "cli-plugin"}

    def test_enable_never_grants_capabilities(self, app):
        from app.models import CapabilityGrant

        _add_plugin("cli-plugin", enabled=False)
        _runner(app).invoke(args=["plugins", "enable", "cli-plugin"])
        grants = CapabilityGrant.query.filter_by(plugin_id="cli-plugin").all()
        assert grants == []


class TestInstallCommand:
    def test_install_local_manifest(self, app, tmp_path):
        path = _write_local_manifest(tmp_path)
        result = _runner(app).invoke(args=["plugins", "install", path])
        assert result.exit_code == 0
        plugin = db.session.get(Plugin, "cli-plugin")
        assert plugin is not None
        assert plugin.enabled is True
        assert plugin.capabilities == ["PROJECT_READ"]

    def test_install_is_idempotent(self, app, tmp_path):
        path = _write_local_manifest(tmp_path)
        assert _runner(app).invoke(args=["plugins", "install", path]).exit_code == 0
        again = _runner(app).invoke(args=["plugins", "install", path])
        assert again.exit_code == 0
        assert db.session.query(Plugin).filter_by(id="cli-plugin").count() == 1

    def test_install_json(self, app, tmp_path):
        path = _write_local_manifest(tmp_path)
        result = _runner(app).invoke(args=["plugins", "install", path, "--json"])
        assert result.exit_code == 0
        data = json.loads(result.output)
        assert data["id"] == "cli-plugin"
        assert data["capabilities"] == ["PROJECT_READ"]

    def test_install_refuses_remote_url(self, app):
        result = _runner(app).invoke(args=["plugins", "install", "https://example.com/plugin"])
        assert result.exit_code == 2
        assert "Remote/URL installation is not supported" in result.output
        assert db.session.get(Plugin, "cli-plugin") is None

    def test_install_invalid_manifest(self, app, tmp_path):
        bad = dict(VALID_MANIFEST)
        del bad["version"]
        path = _write_local_manifest(tmp_path, data=bad)
        result = _runner(app).invoke(args=["plugins", "install", path])
        assert result.exit_code == 2
        assert "Invalid plugin manifest" in result.output
        assert db.session.get(Plugin, "cli-plugin") is None

    def test_install_missing_directory(self, app, tmp_path):
        missing = str(tmp_path / "nope")
        result = _runner(app).invoke(args=["plugins", "install", missing])
        assert result.exit_code == 2
        assert "Manifest file not found" in result.output
