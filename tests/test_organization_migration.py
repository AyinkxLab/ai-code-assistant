"""Organization migration script tests (#636).

The backfill only adds explicit organization admin memberships for workspace
owners. These tests prove the dry run writes nothing, apply is idempotent,
verification succeeds, and rollback removes only the rows the script created.
"""

from app.extensions import db
from app.models import User, Workspace, WorkspaceMember
from app.models.workspace_member import STATUS_ACTIVE
from scripts import organization_migration


def _owner(username, email):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _workspace(owner, name):
    workspace = Workspace(user_id=owner.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _rows():
    return WorkspaceMember.query.count()


class TestPlan:
    def test_dry_run_plans_without_writing(self, app):
        owner = _owner("alice", "alice@example.com")
        _workspace(owner, "Acme platform")
        _workspace(owner, "Acme mobile")

        result = organization_migration.plan_backfill()

        assert result["scanned"] == 2
        assert result["ready"] == 0
        assert len(result["pending"]) == 2
        assert _rows() == 0
        assert "would add admin" in organization_migration.format_plan(result)
        assert "No changes written" in organization_migration.format_plan(result)

    def test_ready_workspace_is_not_planned(self, app):
        owner = _owner("alice", "alice@example.com")
        workspace = _workspace(owner, "Acme platform")
        db.session.add(
            WorkspaceMember(
                workspace_id=workspace.id,
                user_id=owner.id,
                role="admin",
                status=STATUS_ACTIVE,
            )
        )
        db.session.commit()

        result = organization_migration.plan_backfill()

        assert result["ready"] == 1
        assert result["pending"] == []


class TestApplyVerifyRollback:
    def test_apply_is_idempotent_and_verify_passes(self, app):
        owner = _owner("alice", "alice@example.com")
        workspace = _workspace(owner, "Acme platform")

        first = organization_migration.apply_backfill()
        second = organization_migration.apply_backfill()

        assert len(first["created"]) == 1
        assert second["created"] == []
        membership = WorkspaceMember.query.filter_by(workspace_id=workspace.id).one()
        assert membership.user_id == owner.id
        assert membership.role == "admin"
        assert membership.status == STATUS_ACTIVE
        assert organization_migration.verify_backfill()["ok"] is True

    def test_rollback_removes_only_backfilled_rows(self, app):
        owner = _owner("alice", "alice@example.com")
        other = _owner("bob", "bob@example.com")
        workspace = _workspace(owner, "Acme platform")
        db.session.add(
            WorkspaceMember(
                workspace_id=workspace.id,
                user_id=other.id,
                role="viewer",
                status=STATUS_ACTIVE,
            )
        )
        db.session.commit()
        organization_migration.apply_backfill()
        assert _rows() == 2

        result = organization_migration.rollback_backfill()

        assert len(result["removed"]) == 1
        remaining = WorkspaceMember.query.one()
        assert remaining.user_id == other.id
        assert organization_migration.verify_backfill()["ok"] is False
        assert "verification failed" in organization_migration.format_verify(
            organization_migration.verify_backfill()
        )
