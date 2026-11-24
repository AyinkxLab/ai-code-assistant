"""Organization plugin governance tests (#634)."""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import AuditLog, OrganizationPluginPolicy, Plugin, PluginApproval, Workspace
from app.models.workspace_member import ROLE_CONTRIBUTOR
from app.services import audit, plugin_governance

MANIFEST = {
    "id": "governed-plugin",
    "name": "Governed Plugin",
    "version": "0.1.0",
    "description": "A governed test plugin",
    "author": "Tester",
    "entry_point": "plugins.governed:GovernedPlugin",
    "capabilities": ["PROJECT_READ"],
}


def _workspace(owner, name="Org"):
    workspace = Workspace(user_id=owner.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _install(client, workspace_id, manifest=None):
    return client.post(
        f"/plugins/api/workspaces/{workspace_id}/plugins/install",
        json={"manifest": manifest or MANIFEST},
    )


def _set_policy(client, workspace_id, mode):
    return client.put(
        f"/plugins/api/workspaces/{workspace_id}/plugins/policy",
        json={"mode": mode},
    )


class TestModes:
    def test_default_allow_installs_without_a_policy_row(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()

        response = _install(client, workspace.id)

        assert response.status_code == 201
        assert OrganizationPluginPolicy.query.count() == 0
        assert Plugin.query.filter_by(id=MANIFEST["id"]).one() is not None

    def test_allow_mode_installs(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()

        assert _set_policy(client, workspace.id, "allow").status_code == 200
        assert _install(client, workspace.id).status_code == 201

    def test_block_mode_refuses_and_audits_the_attempt(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()
        _set_policy(client, workspace.id, "block")

        response = _install(client, workspace.id)

        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"
        assert Plugin.query.filter_by(id=MANIFEST["id"]).count() == 0
        row = AuditLog.query.filter_by(action=audit.PLUGIN_INSTALL_BLOCKED).one()
        assert row.workspace_id == workspace.id
        assert row.user_id == owner.id
        assert row.metadata_dict["plugin_id"] == MANIFEST["id"]
        assert row.metadata_dict["mode"] == "block"

    def test_require_approval_blocks_until_approved(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()
        _set_policy(client, workspace.id, "require_approval")

        blocked = _install(client, workspace.id)
        assert blocked.status_code == 403
        assert blocked.get_json()["code"] == "forbidden"
        assert Plugin.query.filter_by(id=MANIFEST["id"]).count() == 0

        approval = client.post(
            f"/plugins/api/workspaces/{workspace.id}/plugins/{MANIFEST['id']}/approve",
            json={"reason": "reviewed in security review"},
        )
        assert approval.status_code == 201
        assert approval.get_json()["plugin_id"] == MANIFEST["id"]

        allowed = _install(client, workspace.id)
        assert allowed.status_code == 201
        assert PluginApproval.query.filter_by(plugin_id=MANIFEST["id"]).count() == 1


class TestAudit:
    def test_policy_changes_and_approvals_are_audited(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()

        _set_policy(client, workspace.id, "require_approval")
        client.post(
            f"/plugins/api/workspaces/{workspace.id}/plugins/{MANIFEST['id']}/approve",
            json={"reason": "ok"},
        )

        policy_row = AuditLog.query.filter_by(action=audit.PLUGIN_POLICY_UPDATED).one()
        assert policy_row.user_id == owner.id
        assert policy_row.metadata_dict["mode"] == "require_approval"

        approval_row = AuditLog.query.filter_by(action=audit.PLUGIN_INSTALL_APPROVED).one()
        assert approval_row.user_id == owner.id
        assert approval_row.metadata_dict["plugin_id"] == MANIFEST["id"]


class TestPermissions:
    def test_non_admin_member_cannot_change_policy_or_approve(self, client, make_user, login):
        from app.models import WorkspaceMember

        owner = make_user(username="owner", email="owner@example.com")
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner)
        db.session.add(
            WorkspaceMember(
                workspace_id=workspace.id,
                user_id=member.id,
                role=ROLE_CONTRIBUTOR,
            )
        )
        db.session.commit()
        login(email="member@example.com")

        policy = _set_policy(client, workspace.id, "block")
        approve = client.post(
            f"/plugins/api/workspaces/{workspace.id}/plugins/{MANIFEST['id']}/approve",
            json={},
        )

        assert policy.status_code == 403
        assert policy.get_json()["code"] == "forbidden"
        assert approve.status_code == 403
        assert approve.get_json()["code"] == "forbidden"

    def test_outsider_cannot_read_policy(self, client, make_user, login):
        owner = make_user(username="owner", email="owner@example.com")
        make_user(username="outsider", email="outsider@example.com")
        workspace = _workspace(owner)
        login(email="outsider@example.com")

        response = client.get(f"/plugins/api/workspaces/{workspace.id}/plugins/policy")

        assert response.status_code == 404
        assert response.get_json()["code"] == "not_found"


class TestInvalidInput:
    @pytest.mark.parametrize("mode", [None, "", "sometimes", 3])
    def test_invalid_mode_is_structured(self, client, make_user, login, mode):
        owner = make_user()
        workspace = _workspace(owner)
        login()

        response = client.put(
            f"/plugins/api/workspaces/{workspace.id}/plugins/policy",
            json={"mode": mode},
        )

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"
        assert OrganizationPluginPolicy.query.count() == 0

    def test_non_object_body_is_structured(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()

        response = client.put(
            f"/plugins/api/workspaces/{workspace.id}/plugins/policy",
            json=["allow"],
        )

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"

    def test_invalid_approval_input_is_structured(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()

        response = client.post(
            f"/plugins/api/workspaces/{workspace.id}/plugins/{MANIFEST['id']}/approve",
            json={"reason": 5},
        )

        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"
        assert PluginApproval.query.count() == 0

    def test_service_rejects_invalid_mode_and_plugin_id(self):
        with pytest.raises(plugin_governance.PluginGovernanceError):
            plugin_governance.validate_mode("maybe")
        with pytest.raises(plugin_governance.PluginGovernanceError):
            plugin_governance.validate_plugin_id("")
        with pytest.raises(plugin_governance.PluginGovernanceError):
            plugin_governance.validate_plugin_id("x" * 65)
