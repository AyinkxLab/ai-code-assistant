"""Two-person approval workflow tests (#601)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.extensions import db
from app.models import ApprovalRequest, AuditLog, Workspace, WorkspaceMember
from app.services import approvals, audit

ROLE_CHANGE = {
    "action": "role_change",
    "target_type": "workspace_member",
    "target_id": 7,
    "payload": {"member_id": 7, "from_role": "viewer", "to_role": "contributor"},
}
DATA_DELETION = {
    "action": "data_deletion",
    "target_type": "user",
    "target_id": 7,
    "payload": {"user_id": 7, "scope": "account"},
}


def _workspace(owner) -> Workspace:
    workspace = Workspace(user_id=owner.id, name="Approval Org", slug="approval-org")
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _make_second_admin(make_user, workspace: Workspace):
    from app.models import User

    user = User.query.filter_by(email="second@example.com").first()
    if user is None:
        user = make_user(username="second", email="second@example.com")
    member = WorkspaceMember(
        workspace_id=workspace.id, user_id=user.id, role="admin", status="active"
    )
    db.session.add(member)
    db.session.commit()
    return user


def _create(client, workspace_id: int, body: dict | None = None):
    return client.post(
        "/api/approvals",
        json={"workspace_id": workspace_id, **(body or ROLE_CHANGE)},
    )


def _approve(client, request_id: int):
    return client.post(f"/api/approvals/{request_id}/approve", json={})


def _authorize(client, request_id: int, body: dict | None = None):
    return client.post(f"/api/approvals/{request_id}/authorize", json=body or {})


class TestTwoPersonRule:
    def test_role_change_release_requires_a_distinct_approver(self, client, make_user, login):
        owner = make_user()
        second = _make_second_admin(make_user, _workspace(owner))
        workspace = Workspace.query.filter_by(user_id=owner.id).one()
        login()

        created = _create(client, workspace.id)
        assert created.status_code == 201
        request_id = created.get_json()["id"]
        assert created.get_json()["status"] == "pending"

        self_approve = _approve(client, request_id)
        assert self_approve.status_code == 403
        assert self_approve.get_json()["code"] == "forbidden"

        too_early = _authorize(
            client,
            request_id,
            {"action": "role_change", "workspace_id": workspace.id, "target_id": 7},
        )
        assert too_early.status_code == 409
        assert too_early.get_json()["code"] == "conflict"

        login(email="second@example.com")
        approved = _approve(client, request_id)
        assert approved.status_code == 200
        assert approved.get_json()["decided_by_id"] == second.id
        assert approved.get_json()["status"] == "approved"

        login()
        released = _authorize(
            client,
            request_id,
            {"action": "role_change", "workspace_id": workspace.id, "target_id": 7},
        )
        assert released.status_code == 200
        assert released.get_json()["status"] == "approved"

    def test_data_deletion_request_can_be_rejected_and_never_released(
        self, client, make_user, login
    ):
        owner = make_user()
        _make_second_admin(make_user, _workspace(owner))
        workspace = Workspace.query.filter_by(user_id=owner.id).one()
        login()

        request_id = _create(client, workspace.id, DATA_DELETION).get_json()["id"]
        login(email="second@example.com")
        rejected = client.post(
            f"/api/approvals/{request_id}/reject",
            json={"reason": "not authorized for this account"},
        )
        assert rejected.status_code == 200
        assert rejected.get_json()["decision_reason"] == "not authorized for this account"

        login()
        denied = _authorize(client, request_id, {"action": "data_deletion"})
        assert denied.status_code == 403
        assert denied.get_json()["code"] == "forbidden"


class TestExpiryAuditAndErrors:
    def test_stale_request_expires_and_is_audited(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()

        request_id = _create(client, workspace.id).get_json()["id"]
        row = db.session.get(ApprovalRequest, request_id)
        row.expires_at = datetime.now(UTC) - timedelta(minutes=1)
        db.session.commit()

        listing = client.get(f"/api/approvals?workspace_id={workspace.id}")
        assert listing.status_code == 200
        assert listing.get_json()["items"][0]["status"] == "expired"

        gone = _authorize(client, request_id)
        assert gone.status_code == 410
        assert gone.get_json()["code"] == "gone"

        expired = AuditLog.query.filter_by(action=audit.APPROVAL_EXPIRED).one()
        assert expired.workspace_id == workspace.id
        assert expired.target_id == request_id

    def test_decisions_are_audited(self, client, make_user, login):
        owner = make_user()
        _make_second_admin(make_user, _workspace(owner))
        workspace = Workspace.query.filter_by(user_id=owner.id).one()
        login()
        request_id = _create(client, workspace.id).get_json()["id"]

        login(email="second@example.com")
        assert _approve(client, request_id).status_code == 200

        actions = {row.action for row in AuditLog.query.all()}
        assert {audit.APPROVAL_REQUESTED, audit.APPROVAL_APPROVED} <= actions

    def test_duplicate_pending_request_conflicts(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()

        assert _create(client, workspace.id).status_code == 201
        duplicate = _create(client, workspace.id)
        assert duplicate.status_code == 409
        assert duplicate.get_json()["code"] == "conflict"

    @pytest.mark.parametrize(
        "body",
        [
            {"action": "unknown", "target_type": "workspace_member"},
            {"action": "role_change", "target_type": ""},
            {
                "action": "role_change",
                "target_type": "workspace_member",
                "payload": {"api_key": "x"},
            },
            {"action": "role_change", "target_type": "workspace_member", "ttl_minutes": 0},
        ],
    )
    def test_invalid_input_is_structured(self, client, make_user, login, body):
        owner = make_user()
        workspace = _workspace(owner)
        login()

        response = _create(client, workspace.id, body)
        assert response.status_code == 400
        payload = response.get_json()
        assert payload["code"] == "invalid_input"
        assert payload["kind"] == "invalid"

    def test_non_admin_and_anonymous_callers_are_refused(self, client, make_user):
        owner = make_user()
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner)
        db.session.add(
            WorkspaceMember(
                workspace_id=workspace.id, user_id=member.id, role="viewer", status="active"
            )
        )
        db.session.commit()

        with pytest.raises(approvals.ApprovalPermissionDenied):
            approvals.create_request(
                workspace.id,
                action="role_change",
                target_type="workspace_member",
                actor=member,
            )

        anonymous = _create(client, workspace.id)
        assert anonymous.status_code == 401
        assert anonymous.get_json()["code"] == "unauthorized"


class TestServiceValidation:
    def test_secret_like_payload_keys_are_rejected(self):
        with pytest.raises(approvals.ApprovalError):
            approvals.validate_payload({"password": "plain-text"})

    def test_missing_request_is_not_found(self, make_user):
        owner = make_user()
        _workspace(owner)
        with pytest.raises(approvals.ApprovalNotFound):
            approvals.get_request(999, actor=owner)

    def test_global_admin_flag_does_not_grant_organization_access(self, make_user):
        owner = make_user()
        outsider = make_user(username="outsider", email="outsider@example.com")
        outsider.is_admin = True
        workspace = _workspace(owner)
        db.session.commit()

        with pytest.raises(approvals.ApprovalPermissionDenied):
            approvals.create_request(
                workspace.id,
                action="role_change",
                target_type="workspace_member",
                actor=outsider,
            )
