"""Organization SSO session policy tests (#591)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from app.extensions import db
from app.models import AuditLog, UserSession, Workspace, WorkspaceMember
from app.services import audit, session_policy


def _workspace(owner) -> Workspace:
    workspace = Workspace(user_id=owner.id, name="Policy Org", slug="policy-org")
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _put_policy(client, workspace_id, **kwargs):
    return client.put(f"/api/session-policy/{workspace_id}", json=kwargs)


def _session(user) -> UserSession:
    return (
        UserSession.query.filter_by(user_id=user.id)
        .order_by(UserSession.created_at.desc(), UserSession.id.desc())
        .first()
    )


class TestExpiry:
    def test_maximum_age_revokes_session_with_structured_error(self, client, make_user, login, db):
        owner = make_user()
        workspace = _workspace(owner)
        login()

        assert _put_policy(client, workspace.id, max_session_age_minutes=15).status_code == 200
        row = _session(owner)
        row.created_at = datetime.now(UTC) - timedelta(minutes=20)
        db.session.commit()

        response = client.get("/auth/api/sessions")

        assert response.status_code == 401
        assert response.get_json()["code"] == "unauthorized"
        db.session.refresh(row)
        assert row.revoked_at is not None
        assert row.revoke_reason == session_policy.MAX_AGE_REASON
        assert audit.SESSION_POLICY_EXPIRED in {entry.action for entry in AuditLog.query.all()}

    def test_idle_timeout_revokes_session_with_structured_error(self, client, make_user, login, db):
        owner = make_user()
        workspace = _workspace(owner)
        login()

        assert _put_policy(client, workspace.id, idle_timeout_minutes=15).status_code == 200
        row = _session(owner)
        row.last_seen_at = datetime.now(UTC) - timedelta(minutes=20)
        db.session.commit()

        response = client.get("/auth/api/sessions")

        assert response.status_code == 401
        assert response.get_json()["code"] == "unauthorized"
        db.session.refresh(row)
        assert row.revoke_reason == session_policy.IDLE_REASON

    def test_guard_refreshes_last_seen_and_no_policy_preserves_behavior(
        self, client, make_user, login, db
    ):
        owner = make_user()
        workspace = _workspace(owner)
        login()
        assert client.get("/auth/api/sessions").status_code == 200

        assert _put_policy(client, workspace.id, idle_timeout_minutes=30).status_code == 200
        row = _session(owner)
        before = row.last_seen_at

        assert client.get("/auth/api/sessions").status_code == 200
        db.session.refresh(row)

        assert row.last_seen_at >= before


class TestSensitiveActionReauth:
    def test_sensitive_action_requires_fresh_password(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()
        assert (
            _put_policy(
                client,
                workspace.id,
                require_reauth_for_sensitive_actions=True,
                reauth_window_minutes=15,
            ).status_code
            == 200
        )

        denied = client.post("/auth/api/data-deletion", json={})
        bad_password = client.post(
            f"/api/session-policy/{workspace.id}/reauth", json={"password": "wrong"}
        )

        assert denied.status_code == 401
        assert denied.get_json()["code"] == "unauthorized"
        assert bad_password.status_code == 401
        assert bad_password.get_json()["code"] == "unauthorized"

        refreshed = client.post(
            f"/api/session-policy/{workspace.id}/reauth",
            json={"password": "supersecret123"},
        )
        assert refreshed.status_code == 200
        assert "reauthenticated_at" in refreshed.get_json()
        assert "password" not in refreshed.get_json()

        allowed = client.post("/auth/api/data-deletion", json={})
        assert allowed.status_code != 401

    def test_reauth_marker_expires(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()
        assert (
            _put_policy(
                client,
                workspace.id,
                require_reauth_for_sensitive_actions=True,
                reauth_window_minutes=15,
            ).status_code
            == 200
        )
        assert (
            client.post(
                f"/api/session-policy/{workspace.id}/reauth",
                json={"password": "supersecret123"},
            ).status_code
            == 200
        )
        with client.session_transaction() as current_session:
            current_session[session_policy.REAUTH_SESSION_KEY] = (
                datetime.now(UTC) - timedelta(minutes=20)
            ).isoformat()

        denied = client.post("/auth/api/data-deletion", json={})

        assert denied.status_code == 401
        assert denied.get_json()["code"] == "unauthorized"


class TestPolicyAdministration:
    def test_non_admin_and_global_admin_are_refused(self, client, make_user, login, db):
        owner = make_user()
        member = make_user(username="member", email="member@example.com")
        outsider = make_user(username="outsider", email="outsider@example.com")
        outsider.is_admin = True
        workspace = _workspace(owner)
        db.session.add(
            WorkspaceMember(
                workspace_id=workspace.id, user_id=member.id, role="viewer", status="active"
            )
        )
        db.session.commit()

        login(email="member@example.com")
        member_response = _put_policy(client, workspace.id, idle_timeout_minutes=30)
        login(email="outsider@example.com")
        global_admin_response = _put_policy(client, workspace.id, idle_timeout_minutes=30)

        assert member_response.status_code == 403
        assert member_response.get_json()["code"] == "forbidden"
        assert global_admin_response.status_code == 403
        assert global_admin_response.get_json()["code"] == "forbidden"

    def test_invalid_input_and_unknown_workspace_return_structured_errors(
        self, client, make_user, login
    ):
        owner = make_user()
        workspace = _workspace(owner)
        login()

        invalid = _put_policy(client, workspace.id, max_session_age_minutes=0)
        missing = client.get("/api/session-policy/999999")
        empty_reauth = client.post(f"/api/session-policy/{workspace.id}/reauth", json={})

        assert invalid.status_code == 400
        assert invalid.get_json()["code"] == "invalid_input"
        assert missing.status_code == 404
        assert missing.get_json()["code"] == "not_found"
        assert empty_reauth.status_code == 400
        assert empty_reauth.get_json()["code"] == "invalid_input"

    def test_delete_restores_defaults_and_audits(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()

        created = _put_policy(
            client,
            workspace.id,
            idle_timeout_minutes=30,
            require_reauth_for_sensitive_actions=True,
        )
        fetched = client.get(f"/api/session-policy/{workspace.id}")
        deleted = client.delete(f"/api/session-policy/{workspace.id}")
        after = client.get(f"/api/session-policy/{workspace.id}")

        assert created.status_code == 200
        assert created.get_json()["idle_timeout_minutes"] == 30
        assert fetched.status_code == 200
        assert fetched.get_json()["policy"]["require_reauth_for_sensitive_actions"] is True
        assert deleted.status_code == 204
        assert after.status_code == 200
        assert after.get_json()["policy"] is None
        assert audit.SESSION_POLICY_UPDATED in {entry.action for entry in AuditLog.query.all()}
        assert audit.SESSION_POLICY_DELETED in {entry.action for entry in AuditLog.query.all()}
