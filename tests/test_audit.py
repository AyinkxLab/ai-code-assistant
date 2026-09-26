"""Tests for audit logging of sensitive actions and the admin audit endpoint (#34)."""

from app.extensions import db
from app.models import AuditLog, Conversation, Prompt, User
from app.services import audit


def _register(client, username="auditor", email="auditor@example.com"):
    client.post(
        "/auth/register",
        data={
            "username": username,
            "email": email,
            "password": "supersecret123",
            "password_confirm": "supersecret123",
        },
    )


class TestAuditLogging:
    def test_account_creation_is_logged(self, client, db):
        _register(client)

        entries = AuditLog.query.filter_by(action=audit.USER_CREATED).all()
        assert len(entries) == 1
        entry = entries[0]
        assert entry.target_type == "user"
        assert entry.target_id is not None
        assert entry.metadata_dict["username"] == "auditor"
        assert entry.to_dict()["username"] == "auditor"

    def test_login_success_is_logged(self, client, make_user, login, db):
        make_user(username="auditor", email="auditor@example.com")
        login(email="auditor@example.com")

        assert AuditLog.query.filter_by(action=audit.LOGIN_SUCCESS).count() == 1

    def test_login_failure_is_logged_without_an_actor(self, client, make_user, db):
        make_user(username="auditor", email="auditor@example.com")

        client.post(
            "/auth/login",
            data={"email": "auditor@example.com", "password": "wrong-password"},
        )

        entries = AuditLog.query.filter_by(action=audit.LOGIN_FAILURE).all()
        assert len(entries) == 1
        assert entries[0].user_id is None
        assert entries[0].metadata_dict["email"] == "auditor@example.com"

    def test_conversation_deletion_is_logged(self, client, make_user, login, db):
        user = make_user()
        login()
        conversation = Conversation(user_id=user.id, title="Delete me")
        db.session.add(conversation)
        db.session.commit()
        conversation_id = conversation.id

        response = client.delete(
            f"/chat/conversations/{conversation_id}", headers={"X-CSRFToken": "ignored"}
        )

        assert response.status_code == 200
        entry = AuditLog.query.filter_by(action=audit.CONVERSATION_DELETED).one()
        assert entry.target_type == "conversation"
        assert entry.target_id == conversation_id
        assert entry.metadata_dict["title"] == "Delete me"
        assert entry.user_id == user.id

    def test_prompt_deletion_is_logged(self, client, make_user, login, db):
        user = make_user()
        login()
        prompt = Prompt(user_id=user.id, title="Prompt P", content="content")
        db.session.add(prompt)
        db.session.commit()
        prompt_id = prompt.id

        response = client.delete(
            f"/prompts/api/prompts/{prompt_id}", headers={"X-CSRFToken": "ignored"}
        )

        assert response.status_code == 200
        entry = AuditLog.query.filter_by(action=audit.PROMPT_DELETED).one()
        assert entry.target_type == "prompt"
        assert entry.target_id == prompt_id
        assert entry.metadata_dict["title"] == "Prompt P"
        assert entry.user_id == user.id


class TestAdminAuditEndpoint:
    def _make_admin(self, make_user):
        user = make_user(username="admin", email="admin@example.com")
        user.is_admin = True
        db.session.commit()
        return user

    def _seed_entries(self, admin):
        worker = User(username="worker", email="worker@example.com")
        worker.set_password("supersecret123")
        db.session.add(worker)
        db.session.commit()
        audit.record(
            audit.USER_CREATED,
            user=worker,
            target_type="user",
            target_id=worker.id,
            metadata={"username": worker.username},
        )
        audit.record(
            audit.LOGIN_SUCCESS,
            user=admin,
            target_type="user",
            target_id=admin.id,
            metadata={"email": admin.email},
        )
        db.session.commit()
        return worker

    def test_requires_login(self, client):
        assert client.get("/admin/audit").status_code == 302

    def test_non_admin_is_forbidden(self, client, make_user, login):
        make_user()
        login()

        response = client.get("/admin/audit")

        assert response.status_code == 403
        assert response.get_json()["kind"] == "forbidden"

    def test_admin_lists_entries(self, client, make_user, login):
        admin = self._make_admin(make_user)
        self._seed_entries(admin)
        login(email="admin@example.com")

        response = client.get("/admin/audit")

        assert response.status_code == 200
        payload = response.get_json()
        actions = {item["action"] for item in payload["items"]}
        assert {"user_created", "login_success"} <= actions

    def test_admin_filters_by_action(self, client, make_user, login):
        admin = self._make_admin(make_user)
        self._seed_entries(admin)
        login(email="admin@example.com")

        items = client.get("/admin/audit?action=user_created").get_json()["items"]

        assert [item["action"] for item in items] == ["user_created"]

    def test_admin_filters_by_user_and_username(self, client, make_user, login):
        admin = self._make_admin(make_user)
        worker = self._seed_entries(admin)
        login(email="admin@example.com")

        by_id = client.get(f"/admin/audit?user_id={worker.id}").get_json()["items"]
        assert by_id and {item["user_id"] for item in by_id} == {worker.id}

        by_name = client.get("/admin/audit?username=worker").get_json()["items"]
        assert by_name and {item["user_id"] for item in by_name} == {worker.id}

        # Unknown usernames return nothing rather than leaking account existence.
        assert client.get("/admin/audit?username=ghost").get_json()["items"] == []

    def test_audit_log_is_append_only(self, client, make_user, login):
        self._make_admin(make_user)
        login(email="admin@example.com")

        for method in ("delete", "patch", "post", "put"):
            assert getattr(client, method)("/admin/audit").status_code == 405
