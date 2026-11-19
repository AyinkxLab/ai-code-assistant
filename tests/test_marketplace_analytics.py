"""Privacy-safe marketplace install analytics (#695)."""

from app.extensions import db
from app.models import Plugin, PluginInstallEvent, Workspace
from app.services.marketplace_analytics import (
    aggregate_events,
    analytics_csv,
    install_counts,
    record_plugin_event,
)


def _workspace(owner, name="Analytics WS"):
    workspace = Workspace(user_id=owner.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _plugin(plugin_id="analytics-plugin"):
    plugin = Plugin(
        id=plugin_id,
        name="Analytics Plugin",
        version="1.0.0",
        description="test",
        author="Acme",
        entry_point="plugins.test:TestPlugin",
        capabilities=["PROJECT_READ"],
    )
    db.session.add(plugin)
    db.session.commit()
    return plugin


class TestAnalyticsModel:
    def test_rows_carry_no_identity_columns(self):
        columns = {column.name for column in PluginInstallEvent.__table__.columns}

        assert columns == {"id", "plugin_id", "action", "created_at"}
        assert {"user_id", "workspace_id", "ip", "ip_address", "user_agent"}.isdisjoint(columns)

    def test_invalid_action_is_rejected(self):
        for value in ("", "delete", None):
            try:
                record_plugin_event("analytics-plugin", value)
            except ValueError:
                continue
            raise AssertionError(f"expected ValueError for action {value!r}")


class TestAggregates:
    def test_counts_each_action_per_plugin(self, app):
        _plugin("alpha")
        _plugin("beta")
        record_plugin_event("alpha", "install")
        record_plugin_event("alpha", "install")
        record_plugin_event("alpha", "update")
        record_plugin_event("alpha", "rollback")
        record_plugin_event("beta", "install")

        rows = aggregate_events(["alpha", "beta", "empty"])

        assert rows == [
            {
                "plugin_id": "alpha",
                "installs": 2,
                "updates": 1,
                "rollbacks": 1,
                "total": 4,
            },
            {
                "plugin_id": "beta",
                "installs": 1,
                "updates": 0,
                "rollbacks": 0,
                "total": 1,
            },
            {
                "plugin_id": "empty",
                "installs": 0,
                "updates": 0,
                "rollbacks": 0,
                "total": 0,
            },
        ]
        assert install_counts() == {"alpha": 2, "beta": 1}

    def test_empty_store_returns_empty_aggregate(self, app):
        assert aggregate_events([]) == []

    def test_csv_has_stable_header_and_rows(self):
        text = analytics_csv(
            [
                {
                    "plugin_id": "alpha",
                    "installs": 2,
                    "updates": 1,
                    "rollbacks": 0,
                    "total": 3,
                }
            ]
        )

        assert text.splitlines() == [
            "plugin_id,installs,updates,rollbacks,total",
            "alpha,2,1,0,3",
        ]


class TestRoutes:
    def test_install_route_records_an_anonymous_install_event(self, app, client, make_user, login):
        owner = make_user()
        login()
        workspace = _workspace(owner)
        manifest = {
            "id": "installed-plugin",
            "name": "Installed Plugin",
            "version": "1.0.0",
            "description": "test",
            "author": "Acme",
            "entry_point": "plugins.test:TestPlugin",
            "capabilities": ["PROJECT_READ"],
        }

        response = client.post(
            f"/plugins/api/workspaces/{workspace.id}/plugins/install",
            json={"manifest": manifest},
        )

        assert response.status_code == 201
        events = PluginInstallEvent.query.filter_by(plugin_id="installed-plugin").all()
        assert [event.action for event in events] == ["install"]
        assert not hasattr(events[0], "user_id")

    def test_dashboard_and_csv_show_counts(self, app, client, make_user, login):
        owner = make_user()
        login()
        _workspace(owner)
        _plugin("alpha")
        record_plugin_event("alpha", "install")
        record_plugin_event("alpha", "update")

        page = client.get("/plugins/analytics")
        html = page.get_data(as_text=True)
        assert page.status_code == 200
        assert "alpha" in html
        assert "Installs" in html

        exported = client.get("/plugins/analytics.csv")
        assert exported.status_code == 200
        assert exported.mimetype == "text/csv"
        assert (
            "attachment; filename=marketplace-analytics.csv"
            in exported.headers["Content-Disposition"]
        )
        body = exported.get_data(as_text=True)
        assert body.splitlines()[0] == "plugin_id,installs,updates,rollbacks,total"
        assert "alpha,1,1,0,2" in body

    def test_empty_dashboard_has_an_explicit_empty_state(self, app, client, make_user, login):
        make_user()
        login()

        response = client.get("/plugins/analytics")

        assert response.status_code == 200
        assert "No plugins have registered analytics events yet." in response.get_data(as_text=True)

    def test_dashboard_requires_authentication(self, client):
        response = client.get("/plugins/analytics")
        assert response.status_code in (302, 401)
