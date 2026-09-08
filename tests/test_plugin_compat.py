"""Tests for PEP 440 plugin compatibility checks (#168).

Covers specifier parsing/comparison (``==``, ``>=``, ``~=``, ``!=``, combined
ranges, boundary versions), invalid specifier handling, manifest validation,
and route-level rejection of an incompatible plugin with a clear message.
"""

import pytest

from app.extensions import db
from app.models import Plugin, Workspace
from app.services.plugin_compat import (
    InvalidCompatibilityError,
    app_version,
    compatibility_status,
    is_valid_compatibility,
    parse_compatibility,
    plugin_compatible,
)
from app.services.plugins import ManifestValidationError, PluginManifest

VALID = {
    "id": "compat-plugin",
    "name": "Compat Plugin",
    "version": "1.0.0",
    "description": "test",
    "author": "tester",
    "entry_point": "plugins.test:TestPlugin",
    "capabilities": ["PROJECT_READ"],
}


@pytest.fixture()
def workspace(app, make_user, login):
    user = make_user()
    login()
    ws = Workspace(user_id=user.id, name="Compat workspace")
    db.session.add(ws)
    db.session.commit()
    return ws


def _install(client, workspace_id, manifest):
    return client.post(
        f"/plugins/api/workspaces/{workspace_id}/plugins/install",
        json={"manifest": manifest},
    )


class TestSpecifierComparison:
    @pytest.mark.parametrize(
        ("specifier", "version", "expected"),
        [
            ("==1.0.0", "1.0.0", True),
            ("==1.0.0", "1.0.1", False),
            (">=0.8.0", "0.8.0", True),
            (">=0.8.0", "0.7.9", False),
            ("~=0.8.0", "0.8.5", True),
            ("~=0.8.0", "0.9.0", False),
            ("!=0.9.0", "0.9.0", False),
            ("!=0.9.0", "0.8.0", True),
            (">=0.1.0,<0.5.0", "0.4.9", True),
            (">=0.1.0,<0.5.0", "0.5.0", False),
            (">=0.1.0,!=0.2.0", "0.2.0", False),
            (">=0.1.0,!=0.2.0", "0.3.0", True),
            ("==0.1.*", "0.1.7", True),
            ("==0.1.*", "0.2.0", False),
            ("<0.2.0", "0.1.9", True),
            ("<0.2.0", "0.2.0", False),
            (">=0.8.0rc1", "0.8.0rc2", True),  # prereleases allowed
        ],
    )
    def test_matches(self, specifier, version, expected):
        assert plugin_compatible(specifier, version) is expected

    def test_empty_restriction_matches_everything(self):
        assert plugin_compatible(None, "0.0.1") is True
        assert plugin_compatible("", "99.9.9") is True

    def test_absent_compatibility_is_unrestricted(self):
        status = compatibility_status(None)
        assert status["valid"] is True
        assert status["compatible"] is True

    def test_app_version_is_available(self):
        assert isinstance(app_version(), str)
        assert app_version()


class TestInvalidSpecifiers:
    @pytest.mark.parametrize(
        "specifier",
        ["bogus", ">=", "not a specifier", "1.2.3.4.5", ">=<1.0.0", 123],
    )
    def test_invalid_specifiers_do_not_match(self, specifier):
        assert is_valid_compatibility(specifier) is False
        assert plugin_compatible(specifier, "0.1.0") is False  # fail closed

    def test_parse_invalid_raises(self):
        with pytest.raises(InvalidCompatibilityError):
            parse_compatibility(">>=0.1.0")

    def test_status_flags_invalid(self):
        status = compatibility_status("bogus")
        assert status["valid"] is False
        assert status["compatible"] is False


class TestManifestValidation:
    def test_absent_compatibility_means_any(self):
        manifest = PluginManifest.from_dict(dict(VALID))
        assert manifest.compatibility is None

    def test_valid_compatibility_accepted(self):
        manifest = PluginManifest.from_dict(dict(VALID, compatibility=">=0.8.0"))
        assert manifest.compatibility == ">=0.8.0"

    def test_invalid_compatibility_rejected(self):
        with pytest.raises(ManifestValidationError) as excinfo:
            PluginManifest.from_dict(dict(VALID, compatibility="not-a-specifier"))
        assert "Invalid compatibility specifier" in str(excinfo.value)


class TestInstallEnforcement:
    def test_incompatible_plugin_refused_with_clear_message(self, app, client, workspace):
        app_version_value = app_version()
        resp = _install(
            client,
            workspace.id,
            dict(VALID, compatibility=">=99.0.0"),
        )
        assert resp.status_code == 400
        body = resp.get_json()["error"]
        assert ">=99.0.0" in body
        assert app_version_value in body
        assert db.session.get(Plugin, "compat-plugin") is None

    def test_compatible_plugin_registers_and_exposes_metadata(self, app, client, workspace):
        resp = _install(
            client,
            workspace.id,
            dict(VALID, compatibility=f"<={app_version()}"),
        )
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["compatible_with_app"] is True
        assert data["compatibility"] == f"<={app_version()}"
        assert data["app_version"] == app_version()
        plugin = db.session.get(Plugin, "compat-plugin")
        assert plugin.compatibility == f"<={app_version()}"

    def test_plugin_without_compatibility_registers(self, app, client, workspace):
        resp = _install(client, workspace.id, VALID)
        assert resp.status_code == 201
        data = resp.get_json()
        assert data["compatibility"] == "any"
        assert data["compatible_with_app"] is True
