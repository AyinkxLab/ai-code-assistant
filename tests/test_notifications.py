"""Tests for the notification inbox API and preferences (#148/#150)."""

from app.extensions import db
from app.models import NotificationPreference, User, Workspace, WorkspaceMember
from app.services.notifications import notify


def _create_user(username, email):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


class TestInbox:
    def test_inbox_is_current_user_scoped(self, client, make_user, login):
        alice = _create_user("alice", "alice@example.com")
        bob = _create_user("bob", "bob@example.com")
        notify(alice, "membership", payload={"title": "For alice"})
        notify(bob, "membership", payload={"title": "For bob"})
        login(email="alice@example.com")
        data = client.get("/workspaces/api/notifications").get_json()
        assert data["total"] == 1
        assert data["items"][0]["payload"]["title"] == "For alice"

    def test_unread_filter_and_count(self, client, make_user, login):
        user = make_user()
        notify(user, "mention", payload={"title": "Mention"})
        notify(user, "membership", payload={"title": "Membership"})
        login()
        data = client.get("/workspaces/api/notifications", query_string={"unread": "1"}).get_json()
        assert data["total"] == 2
        assert data["unread_count"] == 2
        count = client.get("/workspaces/api/notifications/count").get_json()
        assert count["unread"] == 2

    def test_mark_read(self, client, make_user, login):
        user = make_user()
        notification = notify(user, "mention", payload={"title": "Read me"})
        login()
        response = client.post(f"/workspaces/api/notifications/{notification.id}/read")
        assert response.status_code == 200
        assert response.get_json()["is_read"] is True
        assert client.get("/workspaces/api/notifications/count").get_json()["unread"] == 0

    def test_mark_other_users_notification_404(self, client, make_user, login):
        other = _create_user("other", "other@example.com")
        notification = notify(other, "mention", payload={"title": "Not yours"})
        make_user()
        login()
        assert (
            client.post(f"/workspaces/api/notifications/{notification.id}/read").status_code == 404
        )

    def test_mark_all_read(self, client, make_user, login):
        user = make_user()
        notify(user, "mention", payload={"title": "A"})
        notify(user, "membership", payload={"title": "B"})
        login()
        response = client.post("/workspaces/api/notifications/read-all")
        assert response.status_code == 200
        assert client.get("/workspaces/api/notifications/count").get_json()["unread"] == 0

    def test_mark_read_is_idempotent(self, client, make_user, login):
        user = make_user()
        notification = notify(user, "mention", payload={"title": "Idem"})
        login()
        assert (
            client.post(f"/workspaces/api/notifications/{notification.id}/read").status_code == 200
        )
        assert (
            client.post(f"/workspaces/api/notifications/{notification.id}/read").status_code == 200
        )

    def test_pagination(self, client, make_user, login):
        user = make_user()
        for i in range(3):
            notify(user, "membership", payload={"title": f"N{i}"})
        login()
        data = client.get(
            "/workspaces/api/notifications", query_string={"per_page": 2, "page": 2}
        ).get_json()
        assert data["total"] == 3
        assert len(data["items"]) == 1


class TestPreferences:
    def test_defaults_are_all_on(self, client, make_user, login):
        make_user()
        login()
        data = client.get("/workspaces/api/notifications/preferences").get_json()
        for key in ("invitations", "mentions", "membership", "ai_events", "shares"):
            assert data[key] is True

    def test_update_preference_persists(self, client, make_user, login):
        make_user()
        login()
        response = client.put(
            "/workspaces/api/notifications/preferences",
            json={"ai_events": False},
        )
        assert response.status_code == 200
        assert response.get_json()["ai_events"] is False
        assert (
            client.get("/workspaces/api/notifications/preferences").get_json()["ai_events"] is False
        )

    def test_preferences_gate_delivery(self, client, app, make_user, login):
        user = make_user()
        login()
        client.put("/workspaces/api/notifications/preferences", json={"mentions": False})
        created = notify(user, "mention", payload={"title": "Suppressed"})
        assert created is None
        always = notify(user, "role_change", payload={"title": "Always sent"})
        assert always is not None

    def test_share_preference_gates_delivery(self, client, app, make_user, login):
        user = make_user()
        login()
        client.put("/workspaces/api/notifications/preferences", json={"shares": False})
        suppressed = notify(user, "share", payload={"title": "Suppressed share"})
        assert suppressed is None

    def test_preference_row_created_on_get(self, client, make_user, login):
        user = make_user()
        login()
        client.get("/workspaces/api/notifications/preferences")
        assert NotificationPreference.query.filter_by(user_id=user.id).count() == 1


class TestMembershipNotifications:
    def test_add_member_creates_membership_notification(self, client, make_user, login, db):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace = Workspace(user_id=owner.id, name="The workspace")
        db.session.add(workspace)
        db.session.commit()
        login(email="owner@example.com")

        response = client.post(
            f"/workspaces/api/workspaces/{workspace.id}/members",
            json={"username": "member", "role": "viewer"},
        )
        assert response.status_code == 201

        from app.models import Notification

        created = Notification.query.filter_by(user_id=member.id, type="membership").one()
        assert created.is_read is False
        assert created.workspace_id == workspace.id

    def test_remove_member_creates_membership_notification(self, client, make_user, login, db):
        owner = make_user(username="owner", email="owner@example.com")
        member = _create_user("member", "member@example.com")
        workspace = Workspace(user_id=owner.id, name="The workspace")
        db.session.add(workspace)
        db.session.commit()
        db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role="viewer"))
        db.session.commit()
        login(email="owner@example.com")

        response = client.delete(f"/workspaces/api/workspaces/{workspace.id}/members/{member.id}")
        assert response.status_code == 200

        from app.models import Notification

        created = Notification.query.filter_by(user_id=member.id, type="membership").one()
        assert created.is_read is False
