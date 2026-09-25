"""Tests for workspace API routes: CRUD, pinning, ownership isolation, and auth."""

from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import Project, Workspace


def _create_workspace(user_id, name="Alice workspace"):
    workspace = Workspace(user_id=user_id, name=name, description="desc")
    db.session.add(workspace)
    db.session.commit()
    return workspace


class TestWorkspaceAuth:
    def test_list_requires_login(self, client):
        response = client.get("/workspaces/")
        assert response.status_code == 302
        assert "/auth/login" in response.headers["Location"]

    def test_api_list_requires_login(self, client):
        response = client.get("/workspaces/api/workspaces")
        assert response.status_code == 302


class TestWorkspaceCRUD:
    def test_create_workspace(self, client, make_user, login):
        make_user()
        login()
        response = client.post(
            "/workspaces/api/workspaces",
            json={"name": "My Workspace", "description": "A description"},
        )
        assert response.status_code == 201
        payload = response.get_json()
        assert payload["name"] == "My Workspace"
        assert payload["description"] == "A description"
        assert payload["project_count"] == 0

    def test_create_requires_name(self, client, make_user, login):
        make_user()
        login()
        response = client.post("/workspaces/api/workspaces", json={"name": "  "})
        assert response.status_code == 400
        assert "name" in response.get_json()["error"].lower()

    def test_list_own_workspaces(self, client, make_user, login):
        user = make_user()
        login()
        _create_workspace(user.id, "First")
        _create_workspace(user.id, "Second")
        response = client.get("/workspaces/api/workspaces")
        assert response.status_code == 200
        payload = response.get_json()
        names = [w["name"] for w in payload["items"]]
        assert names == ["Second", "First"]
        assert payload["total"] == 2
        assert payload["page"] == 1

    def test_rename_workspace(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _create_workspace(user.id)
        response = client.patch(
            f"/workspaces/api/workspaces/{workspace.id}",
            json={"name": "Renamed"},
        )
        assert response.status_code == 200
        assert response.get_json()["name"] == "Renamed"

    def test_delete_workspace(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _create_workspace(user.id)
        response = client.delete(f"/workspaces/api/workspaces/{workspace.id}")
        assert response.status_code == 200
        assert Workspace.query.count() == 0

    def test_pages_render(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _create_workspace(user.id)
        assert client.get("/workspaces/").status_code == 200
        assert client.get(f"/workspaces/{workspace.id}").status_code == 200


class TestWorkspacePin:
    def test_toggle_pin_persists(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _create_workspace(user.id, "Pinnable")
        assert workspace.to_dict()["is_pinned"] is False

        response = client.patch(
            f"/workspaces/api/workspaces/{workspace.id}",
            json={"is_pinned": True},
        )
        assert response.status_code == 200
        assert response.get_json()["is_pinned"] is True
        assert db.session.get(Workspace, workspace.id).is_pinned is True

        # Toggling back off is persisted too.
        toggled_off = client.patch(
            f"/workspaces/api/workspaces/{workspace.id}", json={"is_pinned": False}
        )
        assert toggled_off.get_json()["is_pinned"] is False

    def test_pinned_workspaces_sort_first_then_recent_activity(self, client, make_user, login):
        user = make_user()
        login()
        base = datetime.now(UTC)
        pinned_old = Workspace(
            user_id=user.id, name="PinnedOld", is_pinned=True, updated_at=base - timedelta(days=3)
        )
        pinned_new = Workspace(
            user_id=user.id, name="PinnedNew", is_pinned=True, updated_at=base - timedelta(hours=2)
        )
        plain_new = Workspace(user_id=user.id, name="PlainNew", updated_at=base)
        plain_old = Workspace(user_id=user.id, name="PlainOld", updated_at=base - timedelta(days=1))
        db.session.add_all([pinned_old, pinned_new, plain_new, plain_old])
        db.session.commit()

        names = [w["name"] for w in client.get("/workspaces/api/workspaces").get_json()["items"]]
        assert names == ["PinnedNew", "PinnedOld", "PlainNew", "PlainOld"]

    def test_dashboard_renders_pin_control(self, client, make_user, login):
        user = make_user()
        login()
        workspace = _create_workspace(user.id, "Pinnable")
        html = client.get("/workspaces/").get_data(as_text=True)
        assert 'data-action="toggle-pin"' in html
        assert f'data-id="{workspace.id}"' in html
        assert 'data-pinned="false"' in html


class TestOwnershipIsolation:
    def test_other_users_workspace_is_404(self, client, make_user, login):
        alice = make_user(username="alice", email="alice@example.com")
        make_user(username="bob", email="bob@example.com")
        login(email="bob@example.com")

        workspace = _create_workspace(alice.id)

        assert client.get(f"/workspaces/{workspace.id}").status_code == 404
        assert (
            client.patch(
                f"/workspaces/api/workspaces/{workspace.id}", json={"name": "nope"}
            ).status_code
            == 404
        )
        assert client.delete(f"/workspaces/api/workspaces/{workspace.id}").status_code == 404
        assert client.get(f"/workspaces/api/workspaces/{workspace.id}/projects").status_code == 404

    def test_list_only_shows_own(self, client, make_user, login):
        alice = make_user(username="alice", email="alice@example.com")
        _create_workspace(alice.id, "Alice only")
        make_user(username="bob", email="bob@example.com")
        login(email="bob@example.com")
        response = client.get("/workspaces/api/workspaces")
        payload = response.get_json()
        assert payload["items"] == []
        assert payload["total"] == 0


class TestWorkspaceSearchPagination:
    def _seed(self, db, user_id, count=5):
        base = datetime.now(UTC)
        names = []
        for index in range(count):
            name = f"Workspace {index:02d}"
            names.append(name)
            db.session.add(
                Workspace(
                    user_id=user_id,
                    name=name,
                    description=f"description {index}",
                    updated_at=base - timedelta(minutes=index),
                )
            )
        db.session.commit()
        return names

    def test_search_filters_by_name(self, client, make_user, login, db):
        user = make_user()
        login()
        self._seed(db, user.id)
        items = client.get("/workspaces/api/workspaces?q=Workspace 01").get_json()["items"]
        assert [w["name"] for w in items] == ["Workspace 01"]

    def test_search_filters_by_description_and_is_case_insensitive(
        self, client, make_user, login, db
    ):
        user = make_user()
        login()
        db.session.add(
            Workspace(user_id=user.id, name="Unrelated", description="Stellar contracts work")
        )
        db.session.add(Workspace(user_id=user.id, name="Other", description="nothing here"))
        db.session.commit()
        items = client.get("/workspaces/api/workspaces?q=STELLAR").get_json()["items"]
        assert [w["name"] for w in items] == ["Unrelated"]

    def test_search_with_no_match_returns_empty(self, client, make_user, login, db):
        user = make_user()
        login()
        self._seed(db, user.id, count=2)
        payload = client.get("/workspaces/api/workspaces?q=zzz-no-match").get_json()
        assert payload["items"] == []
        assert payload["total"] == 0

    def test_pagination_caps_items_and_reports_total(self, client, make_user, login, db):
        user = make_user()
        login()
        self._seed(db, user.id, count=5)

        first = client.get("/workspaces/api/workspaces?page=1&per_page=2").get_json()
        assert first["total"] == 5
        assert first["page"] == 1
        assert first["per_page"] == 2
        assert [w["name"] for w in first["items"]] == ["Workspace 00", "Workspace 01"]

        second = client.get("/workspaces/api/workspaces?page=2&per_page=2").get_json()
        assert [w["name"] for w in second["items"]] == ["Workspace 02", "Workspace 03"]

        third = client.get("/workspaces/api/workspaces?page=3&per_page=2").get_json()
        assert [w["name"] for w in third["items"]] == ["Workspace 04"]

    def test_per_page_is_capped(self, client, make_user, login, db):
        user = make_user()
        login()
        self._seed(db, user.id, count=1)
        payload = client.get("/workspaces/api/workspaces?per_page=500").get_json()
        assert payload["per_page"] == 100

    def test_search_and_pagination_combine(self, client, make_user, login, db):
        user = make_user()
        login()
        self._seed(db, user.id, count=5)
        payload = client.get("/workspaces/api/workspaces?q=Workspace&page=2&per_page=2").get_json()
        assert payload["total"] == 5
        assert [w["name"] for w in payload["items"]] == ["Workspace 02", "Workspace 03"]

    def test_project_count_is_accurate_across_pages(self, client, make_user, login, db):
        user = make_user()
        login()
        names = self._seed(db, user.id, count=2)
        target = Workspace.query.filter_by(user_id=user.id, name=names[0]).one()
        db.session.add_all(
            [
                Project(workspace_id=target.id, user_id=user.id, name="one"),
                Project(workspace_id=target.id, user_id=user.id, name="two"),
            ]
        )
        db.session.commit()
        payload = client.get("/workspaces/api/workspaces?page=1&per_page=1").get_json()
        # names[0] is the most recently updated, so it is the first item.
        assert payload["items"][0]["name"] == names[0]
        assert payload["items"][0]["project_count"] == 2
