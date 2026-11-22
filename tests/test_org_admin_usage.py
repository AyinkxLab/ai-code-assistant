"""Organization usage and budget rollup tests (#633)."""

from org_admin_helpers import _member, _usage, _user, _workspace

from app.extensions import db
from app.models import Budget


class TestUsageRollups:
    def test_admin_member_can_read_usage(self, client, login):
        owner = _user("owner", "owner@example.com")
        admin = _user("orgadmin", "orgadmin@example.com")
        workspace = _workspace(owner)
        _member(workspace, admin, role="admin")
        login(email="orgadmin@example.com")

        assert client.get(f"/admin/organizations/{workspace.id}/usage").status_code == 200

    def test_usage_and_budget_are_labelled_estimates(self, client, login):
        owner = _user("owner", "owner@example.com")
        workspace = _workspace(owner)
        _usage(owner, workspace, tokens=42, cost=0.25)
        db.session.add(Budget(workspace_id=workspace.id, token_limit=1000, cost_limit=10.0))
        db.session.commit()
        login(email="owner@example.com")

        response = client.get(f"/admin/organizations/{workspace.id}/usage?days=7")

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "estimated" in html.lower()
        assert "42" in html
        assert "Within budget" in html

    def test_member_cannot_read_usage(self, client, login):
        owner = _user("owner", "owner@example.com")
        viewer = _user("viewer", "viewer@example.com")
        workspace = _workspace(owner)
        _member(workspace, viewer)
        login(email="viewer@example.com")

        response = client.get(f"/admin/organizations/{workspace.id}/usage")

        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"
