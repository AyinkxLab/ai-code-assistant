"""Role simulation and preview tests (#602).

The preview is read-only: it resolves organization, team, and workspace roles
without changing any assignment. Capability names stay in their existing
namespaces, and every returned capability carries its source role.
"""

from org_admin_helpers import _member, _user, _workspace

from app.extensions import db
from app.models import WorkspaceMember
from app.models.workspace_member import ROLE_CONTRIBUTOR, ROLE_VIEWER


def _capability(payload, namespace, capability):
    entries = payload["capabilities"][namespace]
    matches = [entry for entry in entries if entry["capability"] == capability]
    assert len(matches) == 1
    return matches[0]


def _preview_url(workspace_id, **params):
    params.setdefault("format", "json")
    query = "&".join(f"{key}={value}" for key, value in params.items())
    return f"/admin/organizations/{workspace_id}/roles/preview?{query}"


class TestRolePreview:
    def test_owner_preview_resolves_each_role_layer_with_provenance(self, client, login):
        owner = _user("owner", "owner@example.com")
        viewer = _user("viewer", "viewer@example.com")
        admin = _user("orgadmin", "orgadmin@example.com")
        workspace = _workspace(owner)
        membership = _member(workspace, viewer, role=ROLE_VIEWER)
        _member(workspace, admin, role="admin")
        login(email="owner@example.com")

        viewer_payload = client.get(_preview_url(workspace.id, user_id=viewer.id)).get_json()
        assert viewer_payload["user_id"] == viewer.id
        assert viewer_payload["simulated"] is False
        assert viewer_payload["roles"] == {
            "organization": None,
            "team": ROLE_VIEWER,
            "workspace": None,
            "effective": ROLE_VIEWER,
        }
        assert viewer_payload["membership_id"] == membership.id
        assert _capability(viewer_payload, "workspace", "view_members")["sources"] == [
            {"source": "team_role", "role": ROLE_VIEWER}
        ]
        assert _capability(viewer_payload, "plugin", "project:read")["sources"] == [
            {"source": "team_role", "role": ROLE_VIEWER}
        ]
        assert viewer_payload["capabilities"]["organization"] == []

        owner_payload = client.get(_preview_url(workspace.id, user_id=owner.id)).get_json()
        assert owner_payload["roles"] == {
            "organization": "owner",
            "team": None,
            "workspace": "owner",
            "effective": "owner",
        }
        assert _capability(owner_payload, "workspace", "manage_members")["sources"] == [
            {"source": "workspace_owner", "role": "owner"}
        ]
        org_capability = _capability(owner_payload, "organization", "administer_organization")
        assert org_capability["sources"] == [{"source": "organization_role", "role": "owner"}]

        admin_payload = client.get(_preview_url(workspace.id, user_id=admin.id)).get_json()
        assert admin_payload["roles"]["organization"] == "admin"
        assert _capability(admin_payload, "organization", "administer_organization")["sources"] == [
            {"source": "organization_role", "role": "admin"}
        ]

    def test_capability_namespaces_are_not_flattened(self, client, login):
        owner = _user("owner", "owner@example.com")
        viewer = _user("viewer", "viewer@example.com")
        workspace = _workspace(owner)
        _member(workspace, viewer, role=ROLE_VIEWER)
        login(email="owner@example.com")

        payload = client.get(_preview_url(workspace.id, user_id=viewer.id)).get_json()

        workspace_names = [entry["capability"] for entry in payload["capabilities"]["workspace"]]
        plugin_names = [entry["capability"] for entry in payload["capabilities"]["plugin"]]
        assert workspace_names and plugin_names
        assert all(":" not in name for name in workspace_names)
        assert all(":" in name for name in plugin_names)
        assert all(
            entry["namespace"] == "workspace" for entry in payload["capabilities"]["workspace"]
        )
        assert all(entry["namespace"] == "plugin" for entry in payload["capabilities"]["plugin"])
        assert payload["capability_count"] == (
            len(payload["capabilities"]["workspace"])
            + len(payload["capabilities"]["organization"])
            + len(payload["capabilities"]["plugin"])
        )

    def test_simulated_role_is_calculated_without_writing_assignments(self, client, login):
        owner = _user("owner", "owner@example.com")
        viewer = _user("viewer", "viewer@example.com")
        workspace = _workspace(owner)
        membership = _member(workspace, viewer, role=ROLE_VIEWER)
        login(email="owner@example.com")

        response = client.get(_preview_url(workspace.id, user_id=viewer.id, role=ROLE_CONTRIBUTOR))
        assert response.status_code == 200
        payload = response.get_json()
        assert payload["simulated"] is True
        assert payload["simulated_role"] == ROLE_CONTRIBUTOR
        assert payload["roles"]["effective"] == ROLE_CONTRIBUTOR
        assert payload["roles"]["team"] == ROLE_VIEWER
        assert _capability(payload, "workspace", "comment")["sources"] == [
            {"source": "simulated_role", "role": ROLE_CONTRIBUTOR}
        ]
        assert _capability(payload, "plugin", "project:write")
        assert payload["capabilities"]["organization"] == []

        db.session.refresh(membership)
        assert membership.role == ROLE_VIEWER
        db.session.refresh(workspace)
        assert workspace.user_id == owner.id
        assert (
            WorkspaceMember.query.filter_by(workspace_id=workspace.id, user_id=viewer.id).count()
            == 1
        )

    def test_simulation_does_not_remove_real_organization_role(self, client, login):
        owner = _user("owner", "owner@example.com")
        workspace = _workspace(owner)
        login(email="owner@example.com")

        payload = client.get(
            _preview_url(workspace.id, user_id=owner.id, role=ROLE_VIEWER)
        ).get_json()

        assert payload["roles"]["organization"] == "owner"
        assert payload["roles"]["effective"] == ROLE_VIEWER
        project_read = _capability(payload, "plugin", "project:read")
        assert {source["source"] for source in project_read["sources"]} == {
            "simulated_role",
            "organization_role",
        }
        assert _capability(payload, "plugin", "project:write")["sources"] == [
            {"source": "organization_role", "role": "owner"}
        ]

    def test_owner_can_open_the_admin_ui(self, client, login):
        owner = _user("owner", "owner@example.com")
        viewer = _user("viewer", "viewer@example.com")
        workspace = _workspace(owner)
        _member(workspace, viewer, role=ROLE_VIEWER)
        login(email="owner@example.com")

        response = client.get(
            f"/admin/organizations/{workspace.id}/roles/preview?user_id={viewer.id}"
        )
        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "role preview" in html.lower()
        assert "Effective capabilities for viewer" in html
        assert "team_role: viewer" in html
        assert "project:read" in html

    def test_dashboard_links_to_the_preview(self, client, login):
        owner = _user("owner", "owner@example.com")
        workspace = _workspace(owner)
        login(email="owner@example.com")

        html = client.get(f"/admin/organizations/{workspace.id}").get_data(as_text=True)

        assert f"/admin/organizations/{workspace.id}/roles/preview" in html

    def test_non_admin_is_forbidden(self, client, login):
        owner = _user("owner", "owner@example.com")
        viewer = _user("viewer", "viewer@example.com")
        workspace = _workspace(owner)
        _member(workspace, viewer, role=ROLE_VIEWER)
        login(email="viewer@example.com")

        response = client.get(_preview_url(workspace.id, user_id=owner.id))

        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"

    def test_invalid_input_and_unknown_rows_return_structured_errors(self, client, login):
        owner = _user("owner", "owner@example.com")
        workspace = _workspace(owner)
        login(email="owner@example.com")

        cases = [
            (_preview_url(workspace.id, user_id=owner.id, role="superuser"), 400, "invalid_input"),
            (_preview_url(workspace.id, user_id="not-an-int"), 400, "invalid_input"),
            (_preview_url(workspace.id, user_id=9999), 404, "not_found"),
            (_preview_url(workspace.id), 400, "invalid_input"),
            (_preview_url(9999, user_id=owner.id), 404, "not_found"),
        ]
        for url, status_code, code in cases:
            response = client.get(url)
            assert response.status_code == status_code, url
            assert response.get_json()["code"] == code, url
