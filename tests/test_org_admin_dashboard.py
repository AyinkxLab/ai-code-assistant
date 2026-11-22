"""Organization admin dashboard tests (#626).

The console reuses the workspace model as the organization: the workspace
owner and active ``admin`` members may use it, everyone else is refused with a
structured error. These tests cover the overview cards and authorization.
"""

from org_admin_helpers import _member, _project, _usage, _user, _workspace

from app.extensions import db
from app.models import StellarSecurityFinding


class TestDashboard:
    def test_owner_sees_cards(self, client, login):
        owner = _user("owner", "owner@example.com")
        workspace = _workspace(owner)
        _usage(owner, workspace)
        project = _project(owner, workspace)
        db.session.add(
            StellarSecurityFinding(
                project_id=project.id,
                severity="critical",
                explanation="unchecked authorization",
            )
        )
        db.session.commit()
        login(email="owner@example.com")

        response = client.get(f"/admin/organizations/{workspace.id}")

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "Members" in html
        assert "Workspaces" in html
        assert "Open security actions" in html
        assert "estimated cost" in html
        assert ">1<" in html

    def test_admin_member_can_open_the_console(self, client, login):
        owner = _user("owner", "owner@example.com")
        admin = _user("orgadmin", "orgadmin@example.com")
        workspace = _workspace(owner)
        _member(workspace, admin, role="admin")
        login(email="orgadmin@example.com")

        assert client.get(f"/admin/organizations/{workspace.id}").status_code == 200

    def test_regular_member_is_forbidden(self, client, login):
        owner = _user("owner", "owner@example.com")
        viewer = _user("viewer", "viewer@example.com")
        workspace = _workspace(owner)
        _member(workspace, viewer)
        login(email="viewer@example.com")

        response = client.get(f"/admin/organizations/{workspace.id}")

        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"

    def test_outsider_is_forbidden_and_anonymous_redirected(self, client, login):
        owner = _user("owner", "owner@example.com")
        _user("outsider", "outsider@example.com")
        workspace = _workspace(owner)

        assert client.get(f"/admin/organizations/{workspace.id}").status_code == 302
        login(email="outsider@example.com")
        assert client.get(f"/admin/organizations/{workspace.id}").status_code == 403

    def test_unknown_organization_is_structured_404(self, client, login):
        _user("owner", "owner@example.com")
        login(email="owner@example.com")

        response = client.get("/admin/organizations/9999")

        assert response.status_code == 404
        assert response.get_json()["code"] == "not_found"

    def test_invalid_window_is_structured_400(self, client, login):
        owner = _user("owner", "owner@example.com")
        workspace = _workspace(owner)
        login(email="owner@example.com")

        response = client.get(f"/admin/organizations/{workspace.id}?days=0")

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"
