"""Custom role models, precedence, and least-privilege tests (#599)."""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import RoleAssignment, TeamMember, Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_CONTRIBUTOR, STATUS_ACTIVE
from app.services import permissions, role_assignments
from app.services.role_assignments import RoleAssignmentError


def _workspace(owner, member=None, role=ROLE_CONTRIBUTOR):
    workspace = Workspace(user_id=owner.id, name="WS")
    db.session.add(workspace)
    db.session.commit()
    if member is not None:
        db.session.add(
            WorkspaceMember(
                workspace_id=workspace.id, user_id=member.id, role=role, status=STATUS_ACTIVE
            )
        )
        db.session.commit()
    return workspace


class TestValidation:
    def test_invalid_scope_base_role_and_capability_are_rejected(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        workspace = _workspace(owner)

        with pytest.raises(RoleAssignmentError):
            role_assignments.create_role(workspace, owner, scope="global", name="Bad scope")
        with pytest.raises(RoleAssignmentError):
            role_assignments.create_role(
                workspace, owner, scope="workspace", name="Owner base", base_role="owner"
            )
        with pytest.raises(RoleAssignmentError):
            role_assignments.create_role(
                workspace,
                owner,
                scope="workspace",
                name="Bad capability",
                granted=["delete_everything"],
            )

    def test_workspace_owner_cannot_receive_a_custom_role(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        workspace = _workspace(owner)
        role = role_assignments.create_role(workspace, owner, scope="workspace", name="Restricted")

        with pytest.raises(RoleAssignmentError):
            role_assignments.assign_role(
                workspace,
                owner,
                role_id=role.id,
                user_id=owner.id,
                scope="workspace",
            )


class TestPrecedence:
    def test_workspace_assignment_overrides_team_and_organization(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner, member)
        team = role_assignments.create_team(workspace, owner, name="Team")
        role_assignments.add_team_member(workspace, owner, team_id=team.id, user_id=member.id)

        org_role = role_assignments.create_role(
            workspace, owner, scope="organization", name="Org", base_role="contributor"
        )
        team_role = role_assignments.create_role(
            workspace, owner, scope="team", name="Team", base_role="contributor", team_id=team.id
        )
        workspace_role = role_assignments.create_role(
            workspace,
            owner,
            scope="workspace",
            name="Workspace",
            base_role="viewer",
            granted=["comment"],
            denied=["view_members"],
        )
        role_assignments.assign_role(
            workspace, owner, role_id=org_role.id, user_id=member.id, scope="organization"
        )
        role_assignments.assign_role(
            workspace,
            owner,
            role_id=team_role.id,
            user_id=member.id,
            scope="team",
            team_id=team.id,
        )
        role_assignments.assign_role(
            workspace, owner, role_id=workspace_role.id, user_id=member.id, scope="workspace"
        )

        assignment = role_assignments.effective_assignment(workspace, member)

        assert assignment is not None
        assert assignment.scope == "workspace"
        assert (
            role_assignments.effective_capabilities(workspace, member)
            == assignment.role.capability_set()
        )
        assert permissions.can("comment", workspace.id, member) is True
        assert permissions.can("view_members", workspace.id, member) is False
        assert permissions.can("manage_members", workspace.id, member) is False

    def test_team_assignment_overrides_organization(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner, member)
        team = role_assignments.create_team(workspace, owner, name="Team")
        role_assignments.add_team_member(workspace, owner, team_id=team.id, user_id=member.id)

        org_role = role_assignments.create_role(
            workspace, owner, scope="organization", name="Org", base_role="contributor"
        )
        team_role = role_assignments.create_role(
            workspace,
            owner,
            scope="team",
            name="Team",
            base_role="viewer",
            denied=["view_members"],
            team_id=team.id,
        )
        role_assignments.assign_role(
            workspace, owner, role_id=org_role.id, user_id=member.id, scope="organization"
        )
        role_assignments.assign_role(
            workspace,
            owner,
            role_id=team_role.id,
            user_id=member.id,
            scope="team",
            team_id=team.id,
        )

        assert role_assignments.effective_assignment(workspace, member).scope == "team"
        assert permissions.can("view_members", workspace.id, member) is False


class TestLeastPrivilege:
    def test_same_scope_reassignment_replaces_capabilities(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner, member)
        first = role_assignments.create_role(
            workspace, owner, scope="workspace", name="First", granted=["comment"]
        )
        second = role_assignments.create_role(
            workspace, owner, scope="workspace", name="Second", denied=["comment"]
        )
        role_assignments.assign_role(
            workspace, owner, role_id=first.id, user_id=member.id, scope="workspace"
        )
        role_assignments.assign_role(
            workspace, owner, role_id=second.id, user_id=member.id, scope="workspace"
        )

        rows = RoleAssignment.query.filter_by(
            scope="workspace", scope_id=workspace.id, user_id=member.id
        ).all()
        assert len(rows) == 1
        assert rows[0].role_id == second.id
        assert permissions.can("comment", workspace.id, member) is False

    def test_team_member_without_assignment_gets_viewer_capabilities(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner)
        team = role_assignments.create_team(workspace, owner, name="Team")
        role_assignments.add_team_member(workspace, owner, team_id=team.id, user_id=member.id)

        assert permissions.role_for(workspace.id, member) == "viewer"
        assert permissions.can("view_members", workspace.id, member) is True
        assert permissions.can("manage_members", workspace.id, member) is False

    def test_removed_member_fails_closed_even_with_an_assignment(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner, member)
        role = role_assignments.create_role(
            workspace, owner, scope="workspace", name="Restricted", granted=["comment"]
        )
        role_assignments.assign_role(
            workspace, owner, role_id=role.id, user_id=member.id, scope="workspace"
        )
        membership = WorkspaceMember.query.filter_by(
            workspace_id=workspace.id, user_id=member.id
        ).first()
        membership.mark_removed()
        db.session.commit()

        assert role_assignments.effective_capabilities(workspace, member) is None
        assert permissions.can("comment", workspace.id, member) is False

    def test_revoke_restores_the_builtin_role(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner, member)
        role = role_assignments.create_role(
            workspace, owner, scope="workspace", name="No comments", denied=["comment"]
        )
        role_assignments.assign_role(
            workspace, owner, role_id=role.id, user_id=member.id, scope="workspace"
        )
        assert permissions.can("comment", workspace.id, member) is False

        role_assignments.revoke_role(workspace, owner, user_id=member.id, scope="workspace")

        assert permissions.can("comment", workspace.id, member) is True

    def test_removed_team_member_loses_team_assignment(self, app, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner)
        team = role_assignments.create_team(workspace, owner, name="Team")
        role_assignments.add_team_member(workspace, owner, team_id=team.id, user_id=member.id)
        role = role_assignments.create_role(
            workspace,
            owner,
            scope="team",
            name="Team role",
            base_role="viewer",
            granted=["comment"],
            team_id=team.id,
        )
        role_assignments.assign_role(
            workspace,
            owner,
            role_id=role.id,
            user_id=member.id,
            scope="team",
            team_id=team.id,
        )
        assert permissions.can("comment", workspace.id, member) is True

        team_member = TeamMember.query.filter_by(team_id=team.id, user_id=member.id).first()
        team_member.mark_removed()
        db.session.commit()

        assert role_assignments.effective_capabilities(workspace, member) is None
        assert permissions.can("comment", workspace.id, member) is False
