"""Organization custom-role API tests (#599)."""

from __future__ import annotations

from app.extensions import db
from app.models import Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_CONTRIBUTOR, STATUS_ACTIVE

ROLES_API = "/admin/organizations/{workspace_id}/roles"
MEMBERS_API = "/workspaces/api/workspaces/{workspace_id}/members"


def _workspace(owner, member):
    workspace = Workspace(user_id=owner.id, name="WS")
    db.session.add(workspace)
    db.session.commit()
    db.session.add(
        WorkspaceMember(
            workspace_id=workspace.id,
            user_id=member.id,
            role=ROLE_CONTRIBUTOR,
            status=STATUS_ACTIVE,
        )
    )
    db.session.commit()
    return workspace


def _post(client, workspace_id, payload):
    return client.post(ROLES_API.format(workspace_id=workspace_id), json=payload)


class TestCustomRoleApi:
    def test_non_admin_is_forbidden(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner, member)
        login(email="member@example.com")

        response = client.get(ROLES_API.format(workspace_id=workspace.id))

        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"

    def test_invalid_scope_returns_structured_error(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        workspace = _workspace(owner, make_user(username="member", email="member@example.com"))
        login(email="owner@example.com")

        response = _post(
            client,
            workspace.id,
            {"action": "create_role", "scope": "global", "name": "Bad"},
        )

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"

    def test_assign_and_revoke_change_workspace_access(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner, member)
        login(email="owner@example.com")

        created = _post(
            client,
            workspace.id,
            {
                "action": "create_role",
                "scope": "workspace",
                "name": "No member list",
                "base_role": "viewer",
                "denied_capabilities": ["view_members"],
            },
        )
        assert created.status_code == 200
        role_id = created.get_json()["id"]

        assigned = _post(
            client,
            workspace.id,
            {
                "action": "assign",
                "scope": "workspace",
                "role_id": role_id,
                "user_id": member.id,
            },
        )
        assert assigned.status_code == 200

        login(email="member@example.com")
        denied = client.get(MEMBERS_API.format(workspace_id=workspace.id))
        assert denied.status_code == 403

        login(email="owner@example.com")
        revoked = _post(
            client,
            workspace.id,
            {"action": "revoke", "scope": "workspace", "user_id": member.id},
        )
        assert revoked.status_code == 200

        login(email="member@example.com")
        allowed = client.get(MEMBERS_API.format(workspace_id=workspace.id))
        assert allowed.status_code == 200
