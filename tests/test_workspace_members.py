"""Tests for workspace membership: roles, owner-only management, and isolation."""

from app.extensions import db
from app.models import User, Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_CONTRIBUTOR, ROLE_OWNER, ROLE_VIEWER


def _create_user(username, email):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _workspace_for(user, name="Team workspace"):
    workspace = Workspace(user_id=user.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


class TestMemberManagement:
    def test_add_list_update_remove(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        login(email="owner@example.com")
        workspace = _workspace_for(User.query.filter_by(username="owner").first())

        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/members",
            json={"username": "member", "role": "viewer"},
        )
        assert response.status_code == 201
        assert response.get_json()["role"] == ROLE_VIEWER

        response = client.get(f"/workspaces/api/workspaces/{workspace.id}/members")
        assert response.status_code == 200
        members = response.get_json()
        assert len(members) == 1
        assert members[0]["username"] == "member"

        response = client.patch(
            f"/workspaces/api/workspaces/{workspace.id}/members/{member.id}",
            json={"role": ROLE_CONTRIBUTOR},
        )
        assert response.status_code == 200
        assert response.get_json()["role"] == ROLE_CONTRIBUTOR

        response = client.delete(f"/workspaces/api/workspaces/{workspace.id}/members/{member.id}")
        assert response.status_code == 200
        # Removal is a soft-delete that preserves membership history (#135):
        # the row is retained with status "removed" and excluded from listings.
        row = WorkspaceMember.query.filter_by(workspace_id=workspace.id, user_id=member.id).one()
        assert row.status == "removed"
        assert row.removed_at is not None
        response = client.get(f"/workspaces/api/workspaces/{workspace.id}/members")
        assert response.get_json() == []

    def test_add_unknown_user(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        owner = User.query.filter_by(username="owner").first()
        workspace = _workspace_for(owner)
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/members",
            json={"username": "ghost", "role": "viewer"},
        )
        assert response.status_code == 404

    def test_add_duplicate_member(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        login(email="owner@example.com")
        owner = User.query.filter_by(username="owner").first()
        workspace = _workspace_for(owner)
        db.session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role=ROLE_VIEWER)
        )
        db.session.commit()
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/members",
            json={"username": "member", "role": "viewer"},
        )
        assert response.status_code == 409

    def test_add_self_rejected(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        owner = User.query.filter_by(username="owner").first()
        workspace = _workspace_for(owner)
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/members",
            json={"username": "owner", "role": "viewer"},
        )
        assert response.status_code == 400

    def test_invalid_role_rejected(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        owner = User.query.filter_by(username="owner").first()
        workspace = _workspace_for(owner)
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/members",
            json={"username": "ghost", "role": "admin"},
        )
        assert response.status_code == 400

    def test_owner_role_not_assignable(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        owner = User.query.filter_by(username="owner").first()
        workspace = _workspace_for(owner)
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/members",
            json={"username": "ghost", "role": ROLE_OWNER},
        )
        assert response.status_code == 400

    def test_requires_login(self, client):
        assert client.get("/workspaces/api/workspaces/1/members").status_code == 302


class TestOwnerOnlyIsolation:
    def test_non_owner_cannot_manage(self, client, make_user, login):
        owner = _create_user("owner", "owner@example.com")
        member = _create_user("member", "member@example.com")
        make_user(username="viewer", email="viewer@example.com")
        login(email="viewer@example.com")
        workspace = _workspace_for(owner)
        db.session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role=ROLE_VIEWER)
        )
        db.session.commit()
        assert client.get(f"/workspaces/api/workspaces/{workspace.id}/members").status_code == 404
        assert (
            client.post(
                f"/workspaces/api/workspaces/{workspace.id}/members",
                json={"username": "someone", "role": "viewer"},
            ).status_code
            == 404
        )

    def test_owner_workspace_visibility(self, client, make_user, login):
        owner = _create_user("owner", "owner@example.com")
        member = _create_user("member", "member@example.com")
        make_user(username="viewer", email="viewer@example.com")
        login(email="viewer@example.com")
        workspace = _workspace_for(owner)
        db.session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role=ROLE_VIEWER)
        )
        db.session.commit()

        # Workspace owner actions stay owner-scoped: a member (or anyone else)
        # cannot access the workspace through the owner-scoped routes, and a
        # user who is neither owner nor member is equally locked out.
        assert client.get(f"/workspaces/{workspace.id}").status_code == 404

        # A member cannot see workspace list entries owned by others.
        response = client.get("/workspaces/api/workspaces")
        assert response.get_json()["items"] == []

        # A member cannot access the workspace via the owner-scoped API.
        assert client.get(f"/workspaces/api/workspaces/{workspace.id}/projects").status_code == 404

    def test_member_does_not_break_other_users(self, client, make_user, login):
        make_user(username="owner", email="owner@example.com")
        login(email="owner@example.com")
        first_workspace = _workspace_for(User.query.filter_by(username="owner").first())
        other = _create_user("other", "other@example.com")
        second_workspace = _workspace_for(other)

        assert client.get(f"/workspaces/{first_workspace.id}").status_code == 200
        assert client.get(f"/workspaces/{second_workspace.id}").status_code == 404
