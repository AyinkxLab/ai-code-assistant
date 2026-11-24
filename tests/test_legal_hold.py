"""Legal hold model, enforcement, and API tests (#609)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from app.extensions import db
from app.models import AuditLog, LegalHold, Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_VIEWER
from app.services import audit, audit_retention, data_deletion, legal_hold


def _workspace(user, name="Held"):
    workspace = Workspace(user_id=user.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _aged_entry(workspace, days_ago=40):
    entry = audit.record("probe", workspace_id=workspace.id)
    entry.created_at = datetime.now(UTC) - timedelta(days=days_ago)
    db.session.commit()
    return entry


class TestValidation:
    @pytest.mark.parametrize("value", [None, "", "everything", 3])
    def test_invalid_scope_is_rejected(self, value):
        with pytest.raises(legal_hold.LegalHoldError):
            legal_hold.validate_scope(value)

    @pytest.mark.parametrize("value", [None, "", "   ", 5])
    def test_invalid_reason_is_rejected(self, value):
        with pytest.raises(legal_hold.LegalHoldError):
            legal_hold.validate_reason(value)

    def test_unknown_workspace_is_not_found(self, db, make_user):
        with pytest.raises(LookupError):
            legal_hold.place_hold(999, scope="audit", reason="case-1")

    def test_non_manager_cannot_place_or_release(self, db, make_user):
        owner = make_user()
        viewer = make_user(username="viewer", email="viewer@example.com")
        workspace = _workspace(owner)
        db.session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=viewer.id, role=ROLE_VIEWER)
        )
        db.session.commit()

        place = legal_hold.place_hold(workspace.id, scope="audit", reason="case-1", actor=owner)
        with pytest.raises(PermissionError):
            legal_hold.place_hold(workspace.id, scope="audit", reason="case-2", actor=viewer)
        with pytest.raises(PermissionError):
            legal_hold.release_hold(place["id"], actor=viewer)


class TestRetentionEnforcement:
    def test_audit_hold_blocks_purge_and_release_allows_it(self, db, make_user):
        workspace = _workspace(make_user())
        _aged_entry(workspace)
        audit_retention.set_policy(workspace.id, retention_days=1)
        hold = legal_hold.place_hold(
            workspace.id, scope=legal_hold.SCOPE_AUDIT, reason="litigation"
        )

        blocked = audit_retention.purge_expired(workspace.id)
        assert blocked["deleted"] == 0
        assert blocked["skipped_reason"] == audit_retention.SKIP_LEGAL_HOLD
        assert AuditLog.query.filter_by(workspace_id=workspace.id).count() >= 1

        legal_hold.release_hold(hold["id"], reason="case closed")
        allowed = audit_retention.purge_expired(workspace.id)
        assert allowed["deleted"] == 1
        assert allowed["skipped_reason"] is None

    def test_workspace_hold_does_not_block_audit_purge(self, db, make_user):
        workspace = _workspace(make_user())
        _aged_entry(workspace)
        audit_retention.set_policy(workspace.id, retention_days=1)
        legal_hold.place_hold(
            workspace.id, scope=legal_hold.SCOPE_WORKSPACE, reason="workspace data"
        )

        result = audit_retention.purge_expired(workspace.id)

        assert result["deleted"] == 1
        assert result["skipped_reason"] is None


class TestDeletionEnforcement:
    def test_workspace_hold_blocks_workspace_deletion_until_released(
        self, client, make_user, login
    ):
        owner = make_user()
        workspace = _workspace(owner)
        hold = legal_hold.place_hold(
            workspace.id,
            scope=legal_hold.SCOPE_WORKSPACE,
            reason="preserve project data",
            actor=owner,
        )
        login()

        blocked = client.delete(f"/workspaces/api/workspaces/{workspace.id}")
        assert blocked.status_code == 409
        assert blocked.get_json()["code"] == "conflict"
        assert db.session.get(Workspace, workspace.id) is not None

        legal_hold.release_hold(hold["id"], actor=owner, reason="released")
        allowed = client.delete(f"/workspaces/api/workspaces/{workspace.id}")
        assert allowed.status_code == 200
        assert db.session.get(Workspace, workspace.id) is None

    def test_workspace_hold_blocks_user_deletion_until_released(self, client, make_user, login):
        user = make_user()
        workspace = _workspace(user)
        hold = legal_hold.place_hold(
            workspace.id,
            scope=legal_hold.SCOPE_WORKSPACE,
            reason="hold on member data",
            actor=user,
        )
        login()
        created = client.post("/auth/api/data-deletion").get_json()

        blocked = client.post(
            f"/auth/api/data-deletion/{created['id']}/confirm",
            json={"confirmation": data_deletion.CONFIRMATION_PHRASE},
        )
        assert blocked.status_code == 409
        assert blocked.get_json()["code"] == "conflict"

        legal_hold.release_hold(hold["id"], actor=user, reason="released")
        allowed = client.post(
            f"/auth/api/data-deletion/{created['id']}/confirm",
            json={"confirmation": data_deletion.CONFIRMATION_PHRASE},
        )
        assert allowed.status_code == 200
        assert allowed.get_json()["status"] == "completed"


class TestLegalHoldApi:
    def test_owner_can_place_list_and_release(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()

        created = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/legal-holds",
            json={"scope": "audit", "reason": "regulator request"},
        )
        assert created.status_code == 201
        body = created.get_json()
        assert body["active"] is True
        assert body["actor_id"] == owner.id
        assert body["scope"] == "audit"

        listed = client.get(f"/workspaces/api/workspaces/{workspace.id}/legal-holds")
        assert listed.status_code == 200
        assert [row["id"] for row in listed.get_json()["holds"]] == [body["id"]]

        released = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/legal-holds/{body['id']}/release",
            json={"reason": "closed"},
        )
        assert released.status_code == 200
        assert released.get_json()["active"] is False
        assert released.get_json()["released_by_id"] == owner.id

        again = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/legal-holds/{body['id']}/release",
            json={},
        )
        assert again.status_code == 409
        assert again.get_json()["code"] == "conflict"

    @pytest.mark.parametrize(
        "payload",
        [
            {"scope": "everything", "reason": "x"},
            {"scope": "audit", "reason": ""},
            {"reason": "missing scope"},
        ],
    )
    def test_invalid_input_is_structured(self, client, make_user, login, payload):
        workspace = _workspace(make_user())
        login()

        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/legal-holds", json=payload
        )

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"
        assert LegalHold.query.filter_by(workspace_id=workspace.id).count() == 0

    def test_non_owner_gets_structured_forbidden(self, client, make_user, login):
        owner = make_user()
        viewer = make_user(username="viewer", email="viewer@example.com")
        workspace = _workspace(owner)
        db.session.add(
            WorkspaceMember(
                workspace_id=workspace.id,
                user_id=viewer.id,
                role=ROLE_VIEWER,
            )
        )
        db.session.commit()
        login(email="viewer@example.com")

        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/legal-holds",
            json={"scope": "audit", "reason": "x"},
        )

        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"

    def test_release_of_another_workspace_hold_is_not_found(self, client, make_user, login):
        owner = make_user()
        one = _workspace(owner, "One")
        two = _workspace(owner, "Two")
        hold = legal_hold.place_hold(one.id, scope="audit", reason="x", actor=owner)
        login()

        response = client.post(
            f"/workspaces/api/workspaces/{two.id}/legal-holds/{hold['id']}/release",
            json={},
        )

        assert response.status_code == 404
        assert response.get_json()["code"] == "not_found"
        assert db.session.get(LegalHold, hold["id"]).is_active is True
