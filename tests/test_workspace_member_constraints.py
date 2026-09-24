"""Regression tests for the ``WorkspaceMember`` data model (#125).

The membership model must enforce a unique ``(workspace_id, user_id)`` pair and
cascade-delete memberships when their workspace (or user) is removed. The
management API is covered in ``tests/test_workspace_members.py``; this file pins
the model/schema invariants directly.
"""

import pytest
from sqlalchemy.exc import IntegrityError

from app.extensions import db
from app.models import User, Workspace, WorkspaceMember
from app.models.workspace_member import (
    MEMBER_ROLES,
    ROLE_CONTRIBUTOR,
    ROLE_OWNER,
    ROLE_VIEWER,
    STATUS_ACTIVE,
    VALID_ROLES,
    VALID_STATUSES,
)


def _user(username, email):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _workspace(owner, name="Workspace"):
    workspace = Workspace(user_id=owner.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _member(workspace, user, role=ROLE_VIEWER):
    member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=role)
    db.session.add(member)
    db.session.commit()
    return member


class TestRoleAndStatusConstants:
    def test_role_constants_are_exactly_owner_contributor_viewer(self):
        assert VALID_ROLES == (ROLE_OWNER, ROLE_CONTRIBUTOR, ROLE_VIEWER)
        # Owner is not assignable to non-owner members.
        assert set(MEMBER_ROLES) == {ROLE_CONTRIBUTOR, ROLE_VIEWER}
        assert ROLE_OWNER not in MEMBER_ROLES

    def test_status_constants(self):
        assert VALID_STATUSES == ("active", "removed")
        assert STATUS_ACTIVE in VALID_STATUSES


class TestUniqueConstraint:
    def test_duplicate_membership_is_rejected(self, app):
        owner = _user("owner", "owner@example.com")
        member = _user("member", "member@example.com")
        workspace = _workspace(owner)
        _member(workspace, member)

        db.session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role=ROLE_VIEWER)
        )
        with pytest.raises(IntegrityError):
            db.session.commit()
        db.session.rollback()

        assert (
            WorkspaceMember.query.filter_by(workspace_id=workspace.id, user_id=member.id).count()
            == 1
        )

    def test_same_user_may_join_different_workspaces(self, app):
        owner = _user("owner2", "owner2@example.com")
        member = _user("member2", "member2@example.com")
        first = _workspace(owner, "First")
        second = _workspace(owner, "Second")
        _member(first, member)
        _member(second, member)

        assert WorkspaceMember.query.filter_by(user_id=member.id).count() == 2


class TestCascadeDelete:
    def test_deleting_workspace_removes_its_members(self, app):
        owner = _user("owner3", "owner3@example.com")
        member = _user("member3", "member3@example.com")
        workspace = _workspace(owner)
        _member(workspace, member)

        db.session.delete(workspace)
        db.session.commit()

        assert WorkspaceMember.query.filter_by(workspace_id=workspace.id).count() == 0

    def test_deleting_one_workspace_leaves_other_members(self, app):
        owner = _user("owner4", "owner4@example.com")
        member = _user("member4", "member4@example.com")
        keep = _workspace(owner, "Keep")
        drop = _workspace(owner, "Drop")
        _member(keep, member)
        _member(drop, member)

        db.session.delete(drop)
        db.session.commit()

        assert WorkspaceMember.query.filter_by(workspace_id=drop.id).count() == 0
        assert WorkspaceMember.query.filter_by(workspace_id=keep.id).count() == 1

    def test_membership_foreign_keys_cascade_on_delete(self):
        # The schema must cascade when a workspace or user row is removed; this
        # is what protects against orphaned memberships on databases that
        # enforce foreign keys (production PostgreSQL).
        for column in (
            WorkspaceMember.__table__.c.workspace_id,
            WorkspaceMember.__table__.c.user_id,
        ):
            assert {fk.ondelete for fk in column.foreign_keys} == {"CASCADE"}
