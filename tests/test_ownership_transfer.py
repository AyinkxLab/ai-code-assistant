"""Tests for workspace ownership transfer (#137)."""

from app.extensions import db
from app.models import Project, User, Workspace, WorkspaceMember
from app.models.activity_event import (
    EVENT_OWNERSHIP_TRANSFERRED,
    ActivityEvent,
)
from app.models.workspace_member import ROLE_OWNER


def _create_user(username, email):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _setup(owner, member):
    workspace = Workspace(user_id=owner.id, name="Transfer workspace")
    db.session.add(workspace)
    db.session.commit()
    db.session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role="contributor")
    )
    project = Project(
        workspace_id=workspace.id,
        user_id=owner.id,
        name="P",
        source="archive",
        status="ready",
    )
    db.session.add(project)
    db.session.commit()
    return workspace, project


class TestOwnershipTransfer:
    def test_transfer_flips_owner_and_project_owner(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace, project = _setup(owner, member)
        login(email="owner@example.com")
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/transfer",
            json={"user_id": member.id},
        )
        assert response.status_code == 200
        db.session.refresh(workspace)
        db.session.refresh(project)
        assert workspace.user_id == member.id
        assert project.user_id == member.id
        old = WorkspaceMember.query.filter_by(workspace_id=workspace.id, user_id=owner.id).one()
        assert old.role == "contributor"
        new = WorkspaceMember.query.filter_by(workspace_id=workspace.id, user_id=member.id).one()
        assert new.role == ROLE_OWNER

    def test_transfer_records_activity(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace, _ = _setup(owner, member)
        login(email="owner@example.com")
        client.post(
            f"/workspaces/api/workspaces/{workspace.id}/transfer",
            json={"user_id": member.id},
        )
        event = ActivityEvent.query.filter_by(
            workspace_id=workspace.id, event_type=EVENT_OWNERSHIP_TRANSFERRED
        ).one()
        assert event.event_metadata["from_username"] == "owner"
        assert event.event_metadata["to_username"] == "member"

    def test_transfer_requires_owner(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace, _ = _setup(owner, member)
        login(email="member@example.com")
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/transfer",
            json={"user_id": owner.id},
        )
        assert response.status_code == 403

    def test_transfer_rejects_self_and_non_members(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        outsider = _create_user("outsider", "outsider@example.com")
        workspace, _ = _setup(owner, member)
        login(email="owner@example.com")
        self_response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/transfer",
            json={"user_id": owner.id},
        )
        assert self_response.status_code == 400
        outside_response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/transfer",
            json={"user_id": outsider.id},
        )
        assert outside_response.status_code == 400

    def test_transfer_rejected_when_target_owns_same_slug(self, client, make_user, login):
        """The target's account already uses this slug, so the move is refused."""
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace, _ = _setup(owner, member)
        # A different workspace owned by the transfer target with the same
        # normalized name; the old schema allowed this, the new one does not.
        db.session.add(Workspace(user_id=member.id, name="transfer  workspace"))
        db.session.commit()
        assert workspace.slug == "transfer-workspace"

        login(email="owner@example.com")
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/transfer",
            json={"user_id": member.id},
        )
        assert response.status_code == 400
        assert "already has a workspace" in response.get_json()["error"]
        # Ownership is unchanged, so the workspace is still usable.
        db.session.refresh(workspace)
        assert workspace.user_id == owner.id

    def test_new_owner_can_manage_after_transfer(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace, _ = _setup(owner, member)
        login(email="owner@example.com")
        client.post(
            f"/workspaces/api/workspaces/{workspace.id}/transfer",
            json={"user_id": member.id},
        )
        _create_user("fresh", "fresh@example.com")
        login(email="member@example.com")
        assert client.get(f"/workspaces/{workspace.id}").status_code == 200
        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/members",
            json={"username": "fresh", "role": "viewer"},
        )
        assert response.status_code == 201
