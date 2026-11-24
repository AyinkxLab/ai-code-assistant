"""Central session revocation tests (#619)."""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import AuditLog, UserSession
from app.services import audit, user_sessions


def _admin(make_user):
    user = make_user(username="admin", email="admin@example.com")
    user.is_admin = True
    db.session.commit()
    return user


def _login_other(app, email="tester@example.com", password="supersecret123"):
    client = app.test_client()
    response = client.post(
        "/auth/login",
        data={"email": email, "password": password},
        follow_redirects=False,
    )
    assert response.status_code == 302
    return client


class TestTracking:
    def test_login_creates_one_session_and_logout_revokes_it(self, client, make_user, login):
        _admin(make_user)
        login(email="admin@example.com")

        listed = client.get("/auth/api/sessions")
        assert listed.status_code == 200
        assert listed.get_json()["count"] == 1

        client.post("/auth/logout")

        assert UserSession.query.count() == 1
        assert UserSession.query.first().is_active is False

    def test_invalid_service_input_is_rejected(self, db, make_user):
        user = make_user()
        with pytest.raises(user_sessions.SessionError):
            user_sessions.bind_current_session(None)
        with pytest.raises(user_sessions.SessionError):
            user_sessions.revoke_all_for_user(user, reason="x" * 256)


class TestSelfRevoke:
    def test_revoke_all_invalidates_the_current_cookie_on_the_next_request(
        self, client, make_user, login
    ):
        user = make_user()
        login()

        response = client.post("/auth/api/sessions/revoke-all")

        assert response.status_code == 200
        assert response.get_json()["revoked"] == 1
        assert all(not row.is_active for row in UserSession.query.filter_by(user_id=user.id))
        assert client.get("/auth/api/sessions").status_code == 401
        assert client.get("/auth/me").status_code == 302

    def test_revoke_all_audits_the_actor_and_reason(self, client, make_user, login):
        user = make_user()
        login()

        client.post("/auth/api/sessions/revoke-all")

        row = AuditLog.query.filter_by(action=audit.SESSIONS_REVOKED).one()
        assert row.user_id == user.id
        assert row.target_id == user.id
        assert row.metadata_dict["reason"] == "self-service revoke-all"
        assert row.metadata_dict["initiated_by"] == "self"


class TestAdminRevoke:
    def test_admin_revokes_every_device_and_audits_actor_and_reason(
        self, app, client, make_user, login
    ):
        admin = _admin(make_user)
        victim = make_user(username="victim", email="victim@example.com")
        victim_client = _login_other(app, email="victim@example.com")
        assert victim_client.get("/auth/me").status_code == 200

        login(email="admin@example.com")
        response = client.post(
            f"/admin/users/{victim.id}/sessions/revoke-all",
            json={"reason": "laptop lost"},
        )

        assert response.status_code == 200
        assert response.get_json()["revoked"] == 1
        assert victim_client.get("/auth/me").status_code == 302

        row = AuditLog.query.filter_by(action=audit.SESSIONS_REVOKED).one()
        assert row.user_id == admin.id
        assert row.target_id == victim.id
        assert row.metadata_dict["reason"] == "laptop lost"
        assert row.metadata_dict["initiated_by"] == "admin"
        assert row.metadata_dict["revoked"] == 1

    def test_non_admin_gets_structured_forbidden(self, client, make_user, login):
        victim = make_user()
        login()

        response = client.post(
            f"/admin/users/{victim.id}/sessions/revoke-all",
            json={"reason": "x"},
        )

        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"

    @pytest.mark.parametrize(
        "payload",
        [{}, {"reason": ""}, {"reason": 3}, {"reason": "x" * 256}],
    )
    def test_invalid_input_is_structured(self, client, make_user, login, payload):
        _admin(make_user)
        victim = make_user(username="victim", email="victim@example.com")
        login(email="admin@example.com")

        response = client.post(
            f"/admin/users/{victim.id}/sessions/revoke-all",
            json=payload,
        )

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"
        assert AuditLog.query.filter_by(action=audit.SESSIONS_REVOKED).count() == 0

    def test_unknown_user_is_not_found(self, client, make_user, login):
        _admin(make_user)
        login(email="admin@example.com")

        response = client.post(
            "/admin/users/999/sessions/revoke-all",
            json={"reason": "x"},
        )

        assert response.status_code == 404
        assert response.get_json()["code"] == "not_found"


class TestLegacyCompatibility:
    def test_sidless_internal_login_still_works(self, client, make_user):
        """A session created without a tracked token is not rejected."""
        user = make_user()
        with client.session_transaction() as flask_session:
            flask_session["_user_id"] = str(user.id)
            flask_session["_fresh"] = True

        assert client.get("/auth/me").status_code == 200
