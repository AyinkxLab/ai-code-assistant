"""Pagination-at-scale tests (#700).

List endpoints stay bounded for large workspaces, oversized page sizes are
clamped, cursors are stable across inserts, and malformed page/cursor values
return a structured 400 instead of a stack trace.
"""

import time

from app.extensions import db
from app.models import ActivityEvent, Workspace
from app.models import Plugin as PluginRow
from app.models.activity_event import EVENT_PLUGIN_EXECUTED
from app.services.pagination import encode_cursor

PLUGIN_COUNT = 1200


def _workspace(owner):
    workspace = Workspace(user_id=owner.id, name="Scale")
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _seed_plugins(count=PLUGIN_COUNT):
    db.session.bulk_save_objects(
        [
            PluginRow(
                id=f"plugin-{index:05d}",
                name=f"Plugin {index}",
                version="1.0.0",
                entry_point="scale:Scale",
                capabilities=[],
                enabled=True,
            )
            for index in range(count)
        ]
    )
    db.session.commit()


def _plugins_url(workspace_id):
    return f"/plugins/api/workspaces/{workspace_id}/plugins"


class TestPluginListPagination:
    def test_oversized_page_size_is_bounded(self, client, make_user, login):
        owner = make_user()
        login()
        workspace = _workspace(owner)
        _seed_plugins(count=150)

        payload = client.get(f"{_plugins_url(workspace.id)}?limit=999999").get_json()

        assert payload["pagination"]["limit"] == 100
        assert len(payload["plugins"]) == 100

    def test_cursor_walks_without_duplicates_or_gaps(self, client, make_user, login):
        owner = make_user()
        login()
        workspace = _workspace(owner)
        _seed_plugins(count=25)

        first = client.get(f"{_plugins_url(workspace.id)}?limit=10").get_json()
        second = client.get(
            f"{_plugins_url(workspace.id)}?limit=10&cursor={first['pagination']['next_cursor']}"
        ).get_json()

        first_ids = [item["id"] for item in first["plugins"]]
        second_ids = [item["id"] for item in second["plugins"]]
        assert first_ids == [f"plugin-{index:05d}" for index in range(10)]
        assert second_ids == [f"plugin-{index:05d}" for index in range(10, 20)]
        assert set(first_ids).isdisjoint(second_ids)

    def test_cursor_is_stable_when_a_row_is_inserted_before_it(self, client, make_user, login):
        owner = make_user()
        login()
        workspace = _workspace(owner)
        _seed_plugins(count=15)

        first = client.get(f"{_plugins_url(workspace.id)}?limit=10").get_json()
        cursor = first["pagination"]["next_cursor"]
        db.session.add(
            PluginRow(
                id="plugin-00000a",
                name="Inserted",
                version="1.0.0",
                entry_point="scale:Scale",
                capabilities=[],
                enabled=True,
            )
        )
        db.session.commit()

        second = client.get(f"{_plugins_url(workspace.id)}?limit=10&cursor={cursor}").get_json()

        assert [item["id"] for item in second["plugins"]] == [
            f"plugin-{index:05d}" for index in range(10, 15)
        ]

    def test_invalid_limit_is_a_structured_400(self, client, make_user, login):
        owner = make_user()
        login()
        workspace = _workspace(owner)

        response = client.get(f"{_plugins_url(workspace.id)}?limit=abc")

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_pagination"

    def test_invalid_cursor_is_a_structured_400(self, client, make_user, login):
        owner = make_user()
        login()
        workspace = _workspace(owner)

        response = client.get(f"{_plugins_url(workspace.id)}?cursor=***")

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_pagination"

    def test_missing_cursor_key_is_a_structured_400(self, client, make_user, login):
        owner = make_user()
        login()
        workspace = _workspace(owner)

        response = client.get(
            f"{_plugins_url(workspace.id)}?cursor={encode_cursor({'wrong': 'shape'})}"
        )

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_pagination"

    def test_many_rows_stay_bounded_and_fast(self, client, make_user, login):
        owner = make_user()
        login()
        workspace = _workspace(owner)
        _seed_plugins()

        started = time.perf_counter()
        response = client.get(f"{_plugins_url(workspace.id)}?limit=50")
        elapsed = time.perf_counter() - started

        assert response.status_code == 200
        payload = response.get_json()
        assert len(payload["plugins"]) == 50
        assert payload["pagination"]["total"] == PLUGIN_COUNT
        assert payload["pagination"]["next_cursor"]
        assert elapsed < 10.0


class TestExecutionLogPaginationAtScale:
    def test_many_audit_rows_are_bounded(self, client, make_user, login):
        owner = make_user()
        login()
        workspace = _workspace(owner)
        db.session.bulk_save_objects(
            [
                ActivityEvent(
                    workspace_id=workspace.id,
                    event_type=EVENT_PLUGIN_EXECUTED,
                    event_metadata={
                        "plugin_id": f"p-{index}",
                        "version": "1.0.0",
                        "capability": "PROJECT_READ",
                        "outcome": "success",
                        "duration_ms": 1,
                    },
                )
                for index in range(1000)
            ]
        )
        db.session.commit()

        response = client.get(
            f"/plugins/api/workspaces/{workspace.id}/plugin-executions?limit=999999"
        )

        assert response.status_code == 200
        payload = response.get_json()
        assert len(payload["items"]) == 100
        assert payload["total"] == 1000
