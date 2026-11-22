"""Tests for the tamper-evident audit hash chain (#606)."""

import json

from app.extensions import db
from app.models import AuditLog, Workspace
from app.services import audit


def _workspace(user, name="Org"):
    workspace = Workspace(user_id=user.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _record(workspace, action="probe"):
    entry = audit.record(action, workspace_id=workspace.id)
    db.session.commit()
    return entry


class TestAuditHashChain:
    def test_entries_store_linked_hashes(self, db, make_user):
        user = make_user()
        workspace = _workspace(user)

        first = _record(workspace)
        second = _record(workspace)
        third = _record(workspace)

        assert first.prev_hash == audit.GENESIS_HASH
        assert second.prev_hash == first.record_hash
        assert third.prev_hash == second.record_hash
        assert all(len(row.record_hash) == 64 for row in (first, second, third))

        result = audit.verify_chain(workspace.id)
        assert result["ok"] is True
        assert result["checked"] == 3
        assert result["unhashed"] == 0

    def test_chains_are_per_workspace(self, db, make_user):
        user = make_user()
        one = _workspace(user, "One")
        two = _workspace(user, "Two")

        a1 = _record(one)
        b1 = _record(two)
        a2 = _record(one)

        assert b1.prev_hash == audit.GENESIS_HASH
        assert a2.prev_hash == a1.record_hash
        assert audit.verify_chain(one.id)["ok"] is True
        assert audit.verify_chain(two.id)["ok"] is True

    def test_global_entries_use_the_null_chain(self, db):
        first = audit.record(audit.LOGIN_FAILURE, metadata={"email": "a@example.com"})
        second = audit.record(audit.LOGIN_FAILURE)
        db.session.commit()

        assert second.prev_hash == first.record_hash
        assert audit.verify_chain(None)["ok"] is True

    def test_modified_row_is_detected(self, db, make_user):
        user = make_user()
        workspace = _workspace(user)
        _record(workspace, "one")
        middle = _record(workspace, "two")
        _record(workspace, "three")

        middle.action = "tampered"
        db.session.commit()

        result = audit.verify_chain(workspace.id)

        assert result["ok"] is False
        assert {"id": middle.id, "problem": audit.PROBLEM_MODIFIED} in result["errors"]

    def test_deleted_middle_row_is_detected(self, db, make_user):
        user = make_user()
        workspace = _workspace(user)
        _record(workspace, "one")
        middle = _record(workspace, "two")
        last = _record(workspace, "three")

        db.session.delete(middle)
        db.session.commit()

        result = audit.verify_chain(workspace.id)

        assert result["ok"] is False
        assert {"id": last.id, "problem": audit.PROBLEM_MISSING} in result["errors"]

    def test_legacy_unhashed_rows_are_not_reported_as_tampered(self, db):
        db.session.add(AuditLog(action="legacy"))
        db.session.commit()

        result = audit.verify_chain(None)

        assert result["ok"] is True
        assert result["unhashed"] == 1
        assert result["checked"] == 0


class TestAuditVerifyCommand:
    def test_verify_reports_intact_chain(self, db, app, make_user):
        user = make_user()
        workspace = _workspace(user)
        _record(workspace)

        result = app.test_cli_runner().invoke(
            args=["audit", "verify", "--workspace-id", str(workspace.id), "--json"]
        )

        assert result.exit_code == 0
        payload = json.loads(result.output)
        assert payload["ok"] is True
        assert payload["chains"][0]["checked"] == 1

    def test_verify_exits_nonzero_when_tampered(self, db, app, make_user):
        user = make_user()
        workspace = _workspace(user)
        entry = _record(workspace)
        entry.action = "tampered"
        db.session.commit()

        result = app.test_cli_runner().invoke(
            args=["audit", "verify", "--workspace-id", str(workspace.id), "--json"]
        )

        assert result.exit_code == 1
        payload = json.loads(result.output)
        assert payload["ok"] is False
        assert payload["chains"][0]["errors"]

    def test_verify_rejects_unknown_workspace(self, db, app):
        result = app.test_cli_runner().invoke(
            args=["audit", "verify", "--workspace-id", "999", "--json"]
        )

        assert result.exit_code == 2
        assert json.loads(result.output) == {
            "error": "Workspace not found.",
            "code": "not_found",
        }
