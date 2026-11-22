"""Tests for the bounded compliance evidence pack (#611)."""

import json

import pytest
from flask import current_app

from app.extensions import db
from app.models import Workspace, WorkspaceMember
from app.services import audit, audit_retention
from app.services.evidence_pack import (
    DISCLAIMER,
    MAX_AUDIT_EVENTS,
    build_evidence_pack,
)


def _workspace(user, name="Org"):
    workspace = Workspace(user_id=user.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


class TestEvidencePack:
    def test_pack_is_evidence_not_a_certification(self, db, make_user):
        workspace = _workspace(make_user())

        pack = build_evidence_pack(workspace.id)

        assert pack["disclaimer"] == DISCLAIMER
        assert pack["certification"] is False
        assert "not a certification" in pack["disclaimer"]
        assert {control["id"] for control in pack["controls"]} == {
            "audit_export",
            "access_review",
            "retention",
            "security_settings",
        }

    def test_audit_export_is_bounded_and_truncated(self, db, make_user):
        workspace = _workspace(make_user())
        for index in range(MAX_AUDIT_EVENTS + 3):
            audit.record(f"probe_{index}", workspace_id=workspace.id)
        db.session.commit()

        pack = build_evidence_pack(workspace.id)

        audit_control = next(c for c in pack["controls"] if c["id"] == "audit_export")
        evidence = audit_control["evidence"]
        assert evidence["total_events"] == MAX_AUDIT_EVENTS + 3
        assert evidence["included_events"] == MAX_AUDIT_EVENTS
        assert evidence["truncated"] is True
        assert evidence["chain_ok"] is True

    def test_confidential_and_personal_fields_are_excluded(self, db, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner)
        db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role="viewer"))
        audit.record(
            "probe",
            workspace_id=workspace.id,
            metadata={"secret": "do-not-export"},
        )
        db.session.commit()

        pack = build_evidence_pack(workspace.id)
        serialized = json.dumps(pack)

        assert "member@example.com" not in serialized
        assert "owner@example.com" not in serialized
        assert '"username"' not in serialized
        assert "do-not-export" not in serialized
        assert '"metadata"' not in serialized

        access = next(c for c in pack["controls"] if c["id"] == "access_review")
        assert access["evidence"]["members"][0]["role"] == "viewer"
        assert access["evidence"]["role_counts"] == {"viewer": 1}

    def test_retention_control_reflects_the_policy(self, db, make_user):
        workspace = _workspace(make_user())
        audit_retention.set_policy(workspace.id, retention_days=45, legal_hold=True)

        pack = build_evidence_pack(workspace.id)

        retention = next(c for c in pack["controls"] if c["id"] == "retention")
        assert retention["evidence"]["retention_days"] == 45
        assert retention["evidence"]["legal_hold"] is True

    def test_pack_never_contains_the_secret_key(self, db, make_user):
        workspace = _workspace(make_user())

        serialized = json.dumps(build_evidence_pack(workspace.id))

        assert current_app.config["SECRET_KEY"] not in serialized

    def test_permission_failure_is_structured(self, db, make_user):
        owner = make_user(username="owner", email="owner@example.com")
        outsider = make_user(username="outsider", email="outsider@example.com")
        workspace = _workspace(owner)

        with pytest.raises(PermissionError, match="permission"):
            build_evidence_pack(workspace.id, actor=outsider)

        assert build_evidence_pack(workspace.id, actor=owner)["workspace"]["id"] == workspace.id

    def test_unknown_workspace_raises_lookup_error(self, db):
        with pytest.raises(LookupError, match="Workspace not found"):
            build_evidence_pack(999)
