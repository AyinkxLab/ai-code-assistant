"""Organization member management tests (#627).

Covers invite, suspend, remove, restore, and role change, plus confirmation,
structured errors, permission refusals, and the empty/loading states.
"""

from datetime import UTC, datetime, timedelta

from org_admin_helpers import _member, _user, _workspace

from app.extensions import db
from app.models import WorkspaceInvitation, WorkspaceMember
from app.models.invitation import INVITE_STATUS_CANCELLED, INVITE_STATUS_PENDING
from app.models.workspace_member import STATUS_ACTIVE, STATUS_REMOVED


class TestMemberManagement:
    def test_admin_member_can_open_members(self, client, login):
        owner = _user("owner", "owner@example.com")
        admin = _user("orgadmin", "orgadmin@example.com")
        workspace = _workspace(owner)
        _member(workspace, admin, role="admin")
        login(email="orgadmin@example.com")

        assert client.get(f"/admin/organizations/{workspace.id}/members").status_code == 200

    def _setup(self, client, login):
        owner = _user("owner", "owner@example.com")
        member = _user("bob", "bob@example.com")
        workspace = _workspace(owner)
        _member(workspace, member, role="viewer")
        login(email="owner@example.com")
        return owner, member, workspace

    def test_members_page_lists_members_and_empty_invitations(self, client, login):
        _, _, workspace = self._setup(client, login)

        response = client.get(f"/admin/organizations/{workspace.id}/members")

        assert response.status_code == 200
        html = response.get_data(as_text=True)
        assert "bob" in html
        assert "No pending invitations." in html
        assert "Inviting..." in html
        assert 'name="confirm"' in html

    def test_invite_creates_pending_invitation(self, client, login):
        _, _, workspace = self._setup(client, login)

        response = client.post(
            f"/admin/organizations/{workspace.id}/members",
            data={"action": "invite", "email": "new@example.com", "role": "admin"},
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert b"Invited new@example.com as admin." in response.data
        invitation = WorkspaceInvitation.query.filter_by(workspace_id=workspace.id).one()
        assert invitation.email == "new@example.com"
        assert invitation.role == "admin"
        assert invitation.status == INVITE_STATUS_PENDING
        assert invitation.token_hash

    def test_invite_invalid_input_returns_structured_errors(self, client, login):
        _, _, workspace = self._setup(client, login)
        url = f"/admin/organizations/{workspace.id}/members"

        bad_email = client.post(url, data={"action": "invite", "email": "nope"})
        bad_role = client.post(
            url, data={"action": "invite", "email": "new@example.com", "role": "superuser"}
        )
        bad_action = client.post(url, data={"action": "explode"})

        for response in (bad_email, bad_role, bad_action):
            assert response.status_code == 400
            assert response.get_json()["code"] == "invalid_input"

    def test_duplicate_invitation_conflicts(self, client, login):
        _, _, workspace = self._setup(client, login)
        url = f"/admin/organizations/{workspace.id}/members"
        client.post(url, data={"action": "invite", "email": "new@example.com"})

        response = client.post(url, data={"action": "invite", "email": "new@example.com"})

        assert response.status_code == 409
        assert response.get_json()["code"] == "conflict"

    def test_suspend_requires_confirmation_and_blocks_access(self, client, login):
        _, member, workspace = self._setup(client, login)
        url = f"/admin/organizations/{workspace.id}/members"

        missing = client.post(url, data={"action": "suspend", "user_id": member.id})
        assert missing.status_code == 400
        assert missing.get_json()["code"] == "invalid_input"

        response = client.post(
            url,
            data={"action": "suspend", "user_id": member.id, "confirm": "yes"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Suspended bob." in response.data
        membership = WorkspaceMember.query.filter_by(
            workspace_id=workspace.id, user_id=member.id
        ).one()
        assert membership.status == STATUS_REMOVED

    def test_remove_cancels_pending_invitations(self, client, login):
        _, member, workspace = self._setup(client, login)
        db.session.add(
            WorkspaceInvitation(
                workspace_id=workspace.id,
                invited_by=member.id,
                email="bob@example.com",
                role="viewer",
                token_hash="a" * 64,
                status=INVITE_STATUS_PENDING,
                expires_at=datetime.now(UTC) + timedelta(days=1),
            )
        )
        db.session.commit()
        url = f"/admin/organizations/{workspace.id}/members"

        response = client.post(
            url,
            data={"action": "remove", "user_id": member.id, "confirm": "yes"},
            follow_redirects=True,
        )

        assert response.status_code == 200
        assert b"Removed bob." in response.data
        invitation = WorkspaceInvitation.query.filter_by(workspace_id=workspace.id).one()
        assert invitation.status == INVITE_STATUS_CANCELLED

    def test_role_change_and_restore(self, client, login):
        _, member, workspace = self._setup(client, login)
        url = f"/admin/organizations/{workspace.id}/members"

        changed = client.post(
            url,
            data={"action": "role", "user_id": member.id, "role": "admin", "confirm": "yes"},
            follow_redirects=True,
        )
        assert changed.status_code == 200
        assert b"Changed role to admin." in changed.data

        client.post(
            url,
            data={"action": "suspend", "user_id": member.id, "confirm": "yes"},
        )
        restored = client.post(
            url,
            data={"action": "reactivate", "user_id": member.id},
            follow_redirects=True,
        )
        assert b"Restored bob." in restored.data
        membership = WorkspaceMember.query.filter_by(
            workspace_id=workspace.id, user_id=member.id
        ).one()
        assert membership.status == STATUS_ACTIVE

    def test_owner_cannot_be_changed(self, client, login):
        owner, _, workspace = self._setup(client, login)
        url = f"/admin/organizations/{workspace.id}/members"

        response = client.post(
            url,
            data={"action": "suspend", "user_id": owner.id, "confirm": "yes"},
        )

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"

    def test_unknown_member_is_404(self, client, login):
        _, _, workspace = self._setup(client, login)
        url = f"/admin/organizations/{workspace.id}/members"

        response = client.post(url, data={"action": "suspend", "user_id": 9999, "confirm": "yes"})

        assert response.status_code == 404
        assert response.get_json()["code"] == "not_found"

    def test_regular_member_cannot_mutate(self, client, login):
        _, _, workspace = self._setup(client, login)
        client.post("/auth/logout")
        client.post(
            "/auth/login",
            data={"email": "bob@example.com", "password": "supersecret123"},
            follow_redirects=True,
        )

        response = client.post(
            f"/admin/organizations/{workspace.id}/members",
            data={"action": "invite", "email": "new@example.com"},
        )

        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"
