"""Tests for audit retention policy, purge, and legal hold (#608)."""

import json
from datetime import UTC, datetime, timedelta

import pytest

from app.extensions import db
from app.models import AuditLog, Workspace
from app.services import audit, audit_retention


def _workspace(user, name="Org"):
    workspace = Workspace(user_id=user.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _aged_entry(workspace, days_ago, action="probe"):
    entry = audit.record(action, workspace_id=workspace.id)
    entry.created_at = datetime.now(UTC) - timedelta(days=days_ago)
    db.session.commit()
    return entry


class TestRetentionValidation:
    @pytest.mark.parametrize("value", [0, -1, 3651, True, "30", None, 1.5])
    def test_invalid_day_counts_are_rejected(self, value):
        with pytest.raises(ValueError):
            audit_retention.validate_retention_days(value)

    @pytest.mark.parametrize("value", [1, 30, 365, 3650])
    def test_valid_day_counts_are_accepted(self, value):
        assert audit_retention.validate_retention_days(value) == value

    def test_set_policy_stores_and_updates(self, db, make_user):
        workspace = _workspace(make_user())

        created = audit_retention.set_policy(workspace.id, retention_days=30)
        assert created["retention_days"] == 30
        assert created["legal_hold"] is False

        updated = audit_retention.set_policy(workspace.id, retention_days=90, legal_hold=True)
        assert updated["retention_days"] == 90
        assert updated["legal_hold"] is True
        assert audit_retention.policy_snapshot(workspace.id)["retention_days"] == 90

    def test_unknown_workspace_is_a_structured_failure(self, db):
        with pytest.raises(LookupError, match="Workspace not found"):
            audit_retention.set_policy(999, retention_days=30)

    def test_non_owner_cannot_change_policy(self, db, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        outsider = make_user(username="outsider", email="outsider@example.com")
        workspace = _workspace(owner)

        audit_retention.set_policy(workspace.id, retention_days=30, actor=owner)

        with pytest.raises(PermissionError, match="permission"):
            audit_retention.set_policy(workspace.id, retention_days=10, actor=outsider)


class TestPurge:
    def test_only_rows_older_than_the_window_are_deleted(self, db, make_user):
        workspace = _workspace(make_user())
        old = _aged_entry(workspace, 400)
        stale = _aged_entry(workspace, 370)
        recent = _aged_entry(workspace, 10)
        old_id, stale_id, recent_id = old.id, stale.id, recent.id
        audit_retention.set_policy(workspace.id, retention_days=365)

        result = audit_retention.purge_expired(workspace.id)

        assert result["deleted"] == 2
        remaining = {row.id for row in AuditLog.query.all()}
        assert remaining == {recent_id}
        assert old_id not in remaining and stale_id not in remaining

    def test_boundary_row_is_retained(self, db, make_user):
        workspace = _workspace(make_user())
        fixed = datetime(2026, 6, 1, tzinfo=UTC)
        boundary = audit.record("boundary", workspace_id=workspace.id)
        boundary.created_at = fixed - timedelta(days=30)
        newer = audit.record("newer", workspace_id=workspace.id)
        newer.created_at = fixed - timedelta(days=29)
        db.session.commit()
        audit_retention.set_policy(workspace.id, retention_days=30)

        result = audit_retention.purge_expired(workspace.id, now=fixed)

        assert result["deleted"] == 0
        assert AuditLog.query.count() == 2

    def test_legal_hold_blocks_every_deletion(self, db, make_user):
        workspace = _workspace(make_user())
        old = _aged_entry(workspace, 1000)
        audit_retention.set_policy(workspace.id, retention_days=1, legal_hold=True)

        result = audit_retention.purge_expired(workspace.id)

        assert result["deleted"] == 0
        assert result["skipped_reason"] == audit_retention.SKIP_LEGAL_HOLD
        assert db.session.get(AuditLog, old.id) is not None

    def test_purge_resumes_after_the_hold_is_released(self, db, make_user):
        workspace = _workspace(make_user())
        _aged_entry(workspace, 1000)
        audit_retention.set_policy(workspace.id, retention_days=1, legal_hold=True)
        audit_retention.set_policy(workspace.id, retention_days=1, legal_hold=False)

        result = audit_retention.purge_expired(workspace.id)

        assert result["deleted"] == 1
        assert result["skipped_reason"] is None

    def test_dry_run_reports_without_deleting(self, db, make_user):
        workspace = _workspace(make_user())
        _aged_entry(workspace, 400)
        audit_retention.set_policy(workspace.id, retention_days=365)

        result = audit_retention.purge_expired(workspace.id, dry_run=True)

        assert result["deleted"] == 1
        assert result["dry_run"] is True
        assert AuditLog.query.count() == 1

    def test_other_workspaces_are_untouched(self, db, make_user):
        user = make_user()
        one = _workspace(user, "One")
        two = _workspace(user, "Two")
        _aged_entry(one, 400)
        kept = _aged_entry(two, 400)
        audit_retention.set_policy(one.id, retention_days=1)

        audit_retention.purge_expired(one.id)

        assert [row.id for row in AuditLog.query.all()] == [kept.id]


class TestRetentionCommands:
    def test_show_and_set(self, db, app, make_user):
        workspace = _workspace(make_user())
        runner = app.test_cli_runner()

        shown = runner.invoke(
            args=["audit", "retention", "show", "--workspace-id", str(workspace.id), "--json"]
        )
        assert shown.exit_code == 0
        assert json.loads(shown.output)["retention_days"] == audit_retention.DEFAULT_RETENTION_DAYS

        updated = runner.invoke(
            args=[
                "audit",
                "retention",
                "set",
                "--workspace-id",
                str(workspace.id),
                "--days",
                "45",
                "--legal-hold",
                "--json",
            ]
        )
        assert updated.exit_code == 0
        assert json.loads(updated.output)["legal_hold"] is True

    def test_set_rejects_invalid_days_with_a_code(self, db, app, make_user):
        workspace = _workspace(make_user())

        result = app.test_cli_runner().invoke(
            args=[
                "audit",
                "retention",
                "set",
                "--workspace-id",
                str(workspace.id),
                "--days",
                "0",
                "--json",
            ]
        )

        assert result.exit_code == 2
        assert json.loads(result.output)["code"] == "invalid_input"

    def test_purge_reports_legal_hold(self, db, app, make_user):
        workspace = _workspace(make_user())
        _aged_entry(workspace, 400)
        audit_retention.set_policy(workspace.id, retention_days=1, legal_hold=True)

        result = app.test_cli_runner().invoke(
            args=["audit", "purge", "--workspace-id", str(workspace.id), "--json"]
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["deleted"] == 0
        assert payload["skipped_reason"] == "legal_hold"
