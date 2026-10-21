"""Tests for the workspace lockfile viewer and marketplace state surfaces (#418, #421)."""

from app.extensions import db
from app.models import Workspace


def _workspace(owner, name="UI WS"):
    workspace = Workspace(user_id=owner.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


class TestPluginManagementPage:
    def test_page_includes_the_lockfile_viewer_and_shared_state_helpers(
        self, app, client, make_user, login
    ):
        owner = make_user()
        login()
        _workspace(owner)

        response = client.get("/plugins/")
        html = response.get_data(as_text=True)

        assert response.status_code == 200
        # Lockfile viewer shell. It is populated client-side from the
        # member-readable `/plugins/api/workspaces/<id>/lockfile` endpoint.
        assert 'id="plugin-lockfile-section"' in html
        assert 'id="plugin-lockfile"' in html
        assert "Lockfile" in html
        assert "Readable by every member" in html
        # Shared empty/error/timeout renderer and retry affordance.
        assert "js/plugin_marketplace.js" in html
        assert "js/plugins.js" in html

    def test_detail_page_uses_the_shared_marketplace_helpers(self, app, client, make_user, login):
        from app.models import Plugin, PluginInstallation

        owner = make_user()
        login()
        workspace = _workspace(owner)
        plugin = Plugin(
            id="ui-plugin",
            name="UI Plugin",
            version="1.0.0",
            description="test",
            author="tester",
            entry_point="plugins.test:TestPlugin",
            capabilities=["PROJECT_READ"],
        )
        db.session.add(plugin)
        db.session.commit()
        db.session.add(PluginInstallation(plugin_id=plugin.id, workspace_id=workspace.id))
        db.session.commit()

        response = client.get(f"/plugins/workspaces/{workspace.id}/plugins/{plugin.id}")
        html = response.get_data(as_text=True)

        assert response.status_code == 200
        assert "js/plugin_marketplace.js" in html


class TestInstallProgress:
    def test_page_includes_a_live_progress_region(self, client, make_user, login):
        owner = make_user()
        login()
        _workspace(owner)

        response = client.get("/plugins/")

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert 'id="plugin-catalog-progress"' in html
        assert 'aria-live="polite"' in html

    def test_shared_helpers_expose_busy_states(self, app):
        from pathlib import Path

        js = (Path(app.root_path) / "static" / "js" / "plugin_marketplace.js").read_text(
            encoding="utf-8"
        )
        assert "function busyOn" in js
        assert "function busyOff" in js
        assert "function busyError" in js
        assert "marketplace-progress" in js

    def test_install_and_catalog_actions_use_the_busy_states(self, app):
        from pathlib import Path

        for name in ("plugins.js", "plugin_catalog.js"):
            js = (Path(app.root_path) / "static" / "js" / name).read_text(encoding="utf-8")
            assert "busyOn" in js
            assert "busyError" in js
