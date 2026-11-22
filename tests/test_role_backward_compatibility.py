"""Backward-compatibility checks for workspace roles (#637).

The capability migration must not change how owners, contributors/admins, and
viewers reach representative API and HTML routes. Denials use the public error
catalog on API paths.
"""

from __future__ import annotations

from app.extensions import db
from app.models import Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_CONTRIBUTOR, ROLE_VIEWER


def _setup(make_user):
    owner = make_user(username="owner", email="owner@example.com")
    contributor = make_user(username="contributor", email="contributor@example.com")
    viewer = make_user(username="viewer", email="viewer@example.com")
    outsider = make_user(username="outsider", email="outsider@example.com")
    workspace = Workspace(user_id=owner.id, name="Compatibility")
    db.session.add(workspace)
    db.session.commit()
    db.session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=contributor.id, role=ROLE_CONTRIBUTOR)
    )
    db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=viewer.id, role=ROLE_VIEWER))
    db.session.commit()
    return workspace, owner, contributor, viewer, outsider


def _login(login, email):
    login(email=email)


class TestApiBackwardCompatibility:
    def test_member_list_is_readable_by_every_role(self, client, make_user, login):
        workspace, owner, contributor, viewer, outsider = _setup(make_user)

        for email in (owner.email, contributor.email, viewer.email):
            _login(login, email)
            response = client.get(f"/workspaces/api/workspaces/{workspace.id}/members")
            assert response.status_code == 200, email

        _login(login, outsider.email)
        denied = client.get(f"/workspaces/api/workspaces/{workspace.id}/members")
        assert denied.status_code == 404
        assert denied.get_json()["code"] == "not_found"

    def test_only_owner_can_add_members(self, client, make_user, login):
        workspace, owner, contributor, viewer, outsider = _setup(make_user)
        payload = {"username": outsider.username, "role": ROLE_VIEWER}

        for email in (contributor.email, viewer.email):
            _login(login, email)
            denied = client.post(f"/workspaces/api/workspaces/{workspace.id}/members", json=payload)
            assert denied.status_code == 403, email
            assert denied.get_json()["code"] == "forbidden"

        _login(login, owner.email)
        allowed = client.post(f"/workspaces/api/workspaces/{workspace.id}/members", json=payload)
        assert allowed.status_code == 201

    def test_only_owner_can_change_settings_and_read_audit(self, client, make_user, login):
        workspace, owner, contributor, viewer, _outsider = _setup(make_user)

        for email in (contributor.email, viewer.email):
            _login(login, email)
            settings = client.put(
                f"/workspaces/api/workspaces/{workspace.id}/settings",
                json={"invitations_enabled": False},
            )
            audit = client.get(f"/workspaces/api/workspaces/{workspace.id}/audit")
            assert settings.status_code == 403
            assert settings.get_json()["code"] == "forbidden"
            assert audit.status_code == 403
            assert audit.get_json()["code"] == "forbidden"

        _login(login, owner.email)
        assert (
            client.put(
                f"/workspaces/api/workspaces/{workspace.id}/settings",
                json={"invitations_enabled": False},
            ).status_code
            == 200
        )
        assert client.get(f"/workspaces/api/workspaces/{workspace.id}/audit").status_code == 200

    def test_billing_is_owner_only(self, client, make_user, login):
        workspace, owner, contributor, viewer, _outsider = _setup(make_user)

        for email in (contributor.email, viewer.email):
            _login(login, email)
            denied = client.get(f"/workspaces/api/workspaces/{workspace.id}/pricing")
            assert denied.status_code == 403
            assert denied.get_json()["code"] == "forbidden"

        _login(login, owner.email)
        assert client.get(f"/workspaces/api/workspaces/{workspace.id}/pricing").status_code == 200

    def test_member_read_and_heartbeat_stay_available_to_members(self, client, make_user, login):
        workspace, owner, contributor, viewer, outsider = _setup(make_user)

        for email in (owner.email, contributor.email, viewer.email):
            _login(login, email)
            assert (
                client.get(f"/workspaces/api/workspaces/{workspace.id}/activity").status_code == 200
            ), email
            assert (
                client.post(f"/workspaces/api/workspaces/{workspace.id}/heartbeat").status_code
                == 200
            ), email

        _login(login, outsider.email)
        assert client.get(f"/workspaces/api/workspaces/{workspace.id}/activity").status_code == 404
        assert (
            client.post(f"/workspaces/api/workspaces/{workspace.id}/heartbeat").status_code == 404
        )


class TestHtmlBackwardCompatibility:
    def test_members_page_serves_every_member_and_hides_from_outsiders(
        self, client, make_user, login
    ):
        workspace, owner, contributor, viewer, outsider = _setup(make_user)

        for email in (owner.email, contributor.email, viewer.email):
            _login(login, email)
            assert client.get(f"/workspaces/{workspace.id}/members").status_code == 200, email

        _login(login, outsider.email)
        assert client.get(f"/workspaces/{workspace.id}/members").status_code == 404

    def test_audit_page_is_owner_only(self, client, make_user, login):
        workspace, owner, contributor, viewer, outsider = _setup(make_user)

        _login(login, owner.email)
        assert client.get(f"/workspaces/{workspace.id}/audit").status_code == 200

        for email in (contributor.email, viewer.email):
            _login(login, email)
            assert client.get(f"/workspaces/{workspace.id}/audit").status_code == 403, email

        _login(login, outsider.email)
        assert client.get(f"/workspaces/{workspace.id}/audit").status_code == 404

    def test_workspace_detail_serves_members_but_not_outsiders(self, client, make_user, login):
        workspace, owner, contributor, viewer, outsider = _setup(make_user)

        for email in (owner.email, contributor.email, viewer.email):
            _login(login, email)
            assert client.get(f"/workspaces/{workspace.id}").status_code == 200, email

        _login(login, outsider.email)
        assert client.get(f"/workspaces/{workspace.id}").status_code == 404

    def test_removed_member_fails_closed(self, client, make_user, login):
        workspace, _owner, contributor, _viewer, _outsider = _setup(make_user)
        membership = WorkspaceMember.query.filter_by(
            workspace_id=workspace.id, user_id=contributor.id
        ).first()
        membership.mark_removed()
        db.session.commit()

        _login(login, contributor.email)
        assert client.get(f"/workspaces/{workspace.id}").status_code == 404
        api = client.get(f"/workspaces/api/workspaces/{workspace.id}/members")
        assert api.status_code == 404
        assert api.get_json()["code"] == "not_found"
