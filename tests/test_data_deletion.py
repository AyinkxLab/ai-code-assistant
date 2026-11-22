"""User data deletion / anonymization workflow tests (#615)."""

from __future__ import annotations

from app.extensions import db
from app.models import (
    AuditLog,
    Conversation,
    Message,
    Prompt,
    User,
    UserDeletionRequest,
    Workspace,
    WorkspaceMember,
)
from app.models.user_deletion_request import (
    MODE_ANONYMIZATION,
    MODE_DELETION,
    STATUS_CANCELLED,
    STATUS_COMPLETED,
    STATUS_REQUESTED,
)
from app.services import audit, data_deletion


def _user(make_user, username="tester", email="tester@example.com"):
    return make_user(username=username, email=email)


def _content(user):
    conversation = Conversation(user_id=user.id, title="Private chat")
    db.session.add(conversation)
    db.session.flush()
    db.session.add(Message(conversation_id=conversation.id, role="user", content="secret"))
    db.session.add(Prompt(user_id=user.id, title="P", content="prompt body"))
    db.session.commit()
    return conversation


class TestDeletionWorkflow:
    def test_happy_path_anonymizes_account_and_deletes_content(self, client, make_user, login):
        user = _user(make_user)
        conversation = _content(user)
        conversation_id = conversation.id
        audit.record(audit.LOGIN_SUCCESS, user=user, target_type="user", target_id=user.id)
        db.session.commit()
        login()

        created = client.post("/auth/api/data-deletion", json={"mode": MODE_DELETION})
        assert created.status_code == 201
        assert created.get_json()["status"] == STATUS_REQUESTED

        confirmed = client.post(
            f"/auth/api/data-deletion/{created.get_json()['id']}/confirm",
            json={"confirmation": data_deletion.CONFIRMATION_PHRASE},
        )

        assert confirmed.status_code == 200
        body = confirmed.get_json()
        assert body["status"] == STATUS_COMPLETED
        assert body["summary"]["deleted"]["conversations"] == 1

        db.session.expire_all()
        deleted_user = db.session.get(User, user.id)
        assert deleted_user.is_active is False
        assert deleted_user.username == f"deleted-user-{user.id}"
        assert deleted_user.email == f"deleted-{user.id}@deleted.invalid"
        assert deleted_user.mfa_secret is None
        assert db.session.get(Conversation, conversation_id) is None
        assert Prompt.query.filter_by(user_id=user.id).count() == 0

    def test_confirmation_phrase_is_required_and_changes_nothing(self, client, make_user, login):
        user = _user(make_user)
        login()
        created = client.post("/auth/api/data-deletion").get_json()

        missing = client.post(f"/auth/api/data-deletion/{created['id']}/confirm", json={})
        wrong = client.post(
            f"/auth/api/data-deletion/{created['id']}/confirm",
            json={"confirmation": "yes"},
        )

        assert missing.status_code == 400
        assert missing.get_json()["code"] == "invalid_input"
        assert wrong.status_code == 400
        row = db.session.get(UserDeletionRequest, created["id"])
        assert row.status == STATUS_REQUESTED
        assert db.session.get(User, user.id).is_active is True

    def test_foreign_user_cannot_read_confirm_or_cancel(self, client, make_user, login):
        owner = _user(make_user)
        make_user(username="other", email="other@example.com")
        row = data_deletion.request_deletion(owner, enforce_rate_limit=False)
        login(email="other@example.com")

        assert client.get(f"/auth/api/data-deletion/{row.id}").status_code == 404
        confirm = client.post(
            f"/auth/api/data-deletion/{row.id}/confirm",
            json={"confirmation": data_deletion.CONFIRMATION_PHRASE},
        )
        cancel = client.post(f"/auth/api/data-deletion/{row.id}/cancel")
        assert confirm.status_code == 404
        assert confirm.get_json()["code"] == "not_found"
        assert cancel.status_code == 404

    def test_already_deleted_account_cannot_request_again(self, app, make_user):
        user = _user(make_user)
        row = data_deletion.request_deletion(user, enforce_rate_limit=False)
        data_deletion.confirm_request(row.id, user, data_deletion.CONFIRMATION_PHRASE)

        try:
            data_deletion.request_deletion(user, enforce_rate_limit=False)
        except data_deletion.AlreadyCompleted:
            pass
        else:  # pragma: no cover - assertion failure path
            raise AssertionError("a completed deletion must refuse a new request")

    def test_cancelled_request_changes_no_account_data(self, client, make_user, login):
        user = _user(make_user)
        login()
        created = client.post("/auth/api/data-deletion").get_json()

        response = client.post(f"/auth/api/data-deletion/{created['id']}/cancel")

        assert response.status_code == 200
        assert response.get_json()["status"] == STATUS_CANCELLED
        assert db.session.get(User, user.id).is_active is True
        assert db.session.get(UserDeletionRequest, created["id"]).status == STATUS_CANCELLED

    def test_invalid_mode_is_structured(self, client, make_user, login):
        _user(make_user)
        login()
        response = client.post("/auth/api/data-deletion", json={"mode": "erase-everything"})
        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"

    def test_requests_are_rate_limited_across_cancellations(
        self, client, make_user, login, monkeypatch
    ):
        _user(make_user)
        login()
        monkeypatch.setattr(data_deletion, "REQUEST_LIMIT", 1)

        first = client.post("/auth/api/data-deletion")
        assert first.status_code == 201
        client.post(f"/auth/api/data-deletion/{first.get_json()['id']}/cancel")
        limited = client.post("/auth/api/data-deletion")

        assert limited.status_code == 429
        assert limited.get_json()["code"] == "rate_limited"
        assert limited.headers["Retry-After"]


class TestAuditPreservation:
    def test_audit_rows_survive_and_record_every_transition(self, app, make_user):
        user = _user(make_user)
        membership_workspace = Workspace(
            user_id=_user(make_user, "owner", "owner@example.com").id, name="Org"
        )
        db.session.add(membership_workspace)
        db.session.commit()
        membership = WorkspaceMember(
            workspace_id=membership_workspace.id, user_id=user.id, role="viewer"
        )
        db.session.add(membership)
        audit.record(audit.LOGIN_SUCCESS, user=user, target_type="user", target_id=user.id)
        db.session.commit()
        before = AuditLog.query.filter_by(user_id=user.id).count()

        row = data_deletion.request_deletion(user, enforce_rate_limit=False)
        data_deletion.confirm_request(row.id, user, data_deletion.CONFIRMATION_PHRASE)

        after_rows = AuditLog.query.filter_by(user_id=user.id).all()
        assert len(after_rows) == before + 3
        assert {entry.action for entry in after_rows} >= {
            audit.LOGIN_SUCCESS,
            audit.USER_DELETION_REQUESTED,
            audit.USER_DELETION_CONFIRMED,
            audit.USER_DELETION_COMPLETED,
        }
        assert all(entry.user_id == user.id for entry in after_rows)
        db.session.refresh(membership)
        assert membership.is_active_member is False

    def test_anonymization_mode_keeps_content(self, app, make_user):
        user = _user(make_user)
        _content(user)
        row = data_deletion.request_deletion(
            user, mode=MODE_ANONYMIZATION, enforce_rate_limit=False
        )

        data_deletion.confirm_request(row.id, user, data_deletion.CONFIRMATION_PHRASE)

        assert db.session.get(User, user.id).username == f"deleted-user-{user.id}"
        assert Conversation.query.filter_by(user_id=user.id).count() == 1
        assert Prompt.query.filter_by(user_id=user.id).count() == 1
