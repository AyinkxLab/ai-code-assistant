"""Per-organization IP allowlist tests (#620)."""

from __future__ import annotations

import pytest

from app.extensions import db
from app.models import AuditLog, Project, Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_CONTRIBUTOR, ROLE_VIEWER
from app.services import audit, ip_allowlist


def _setup(make_user):
    owner = make_user()
    contributor = make_user(username="contributor", email="contributor@example.com")
    viewer = make_user(username="viewer", email="viewer@example.com")
    workspace = Workspace(user_id=owner.id, name="Allowlisted")
    db.session.add(workspace)
    db.session.commit()
    db.session.add(
        WorkspaceMember(workspace_id=workspace.id, user_id=contributor.id, role=ROLE_CONTRIBUTOR)
    )
    db.session.add(WorkspaceMember(workspace_id=workspace.id, user_id=viewer.id, role=ROLE_VIEWER))
    project = Project(
        workspace_id=workspace.id, user_id=owner.id, name="P", source="archive", status="ready"
    )
    db.session.add(project)
    db.session.commit()
    return workspace, owner, contributor, viewer, project


def _get(client, url, ip):
    return client.get(url, environ_base={"REMOTE_ADDR": ip})


def _put(client, url, payload, ip):
    return client.put(url, json=payload, environ_base={"REMOTE_ADDR": ip})


class TestCidrValidation:
    def test_normalizes_single_addresses_and_masks_host_bits(self):
        assert ip_allowlist.normalize_cidrs(["203.0.113.5"]) == ["203.0.113.5/32"]
        assert ip_allowlist.normalize_cidrs(["2001:db8::1"]) == ["2001:db8::1/128"]
        assert ip_allowlist.normalize_cidrs(["10.1.2.3/8"]) == ["10.0.0.0/8"]
        assert ip_allowlist.normalize_cidrs(["10.0.0.0/8", "10.0.0.0/8"]) == ["10.0.0.0/8"]

    @pytest.mark.parametrize(
        "value",
        ["not-a-cidr", "10.0.0.0/33", "2001:db8::/129", "", 42],
    )
    def test_malformed_entries_are_rejected(self, value):
        with pytest.raises(ip_allowlist.AllowlistError):
            ip_allowlist.normalize_cidrs([value])

    def test_non_list_and_oversized_input_are_rejected(self):
        with pytest.raises(ip_allowlist.AllowlistError):
            ip_allowlist.normalize_cidrs("10.0.0.0/8")
        with pytest.raises(ip_allowlist.AllowlistError):
            ip_allowlist.normalize_cidrs(["10.0.0.0/8"] * (ip_allowlist.MAX_CIDRS + 1))


class TestConfigurationApi:
    def test_malformed_cidr_returns_structured_error(self, client, make_user, login):
        workspace, _owner, _contributor, _viewer, _project = _setup(make_user)
        login()
        response = client.put(
            f"/workspaces/api/workspaces/{workspace.id}/ip-allowlist",
            json={"enabled": True, "cidrs": ["10.0.0.0/8", "not-a-cidr"]},
        )
        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"
        assert ip_allowlist.get_config(workspace.id) is None

    def test_enabling_without_ranges_is_rejected(self, client, make_user, login):
        workspace, _owner, _contributor, _viewer, _project = _setup(make_user)
        login()
        response = client.put(
            f"/workspaces/api/workspaces/{workspace.id}/ip-allowlist",
            json={"enabled": True, "cidrs": []},
        )
        assert response.status_code == 400
        assert response.get_json()["code"] == "invalid_input"

    def test_configuration_is_owner_only(self, client, make_user, login):
        workspace, _owner, contributor, viewer, _project = _setup(make_user)
        url = f"/workspaces/api/workspaces/{workspace.id}/ip-allowlist"

        for email in (contributor.email, viewer.email):
            login(email=email)
            read = client.get(url)
            write = client.put(url, json={"enabled": False, "cidrs": []})
            assert read.status_code == 403
            assert read.get_json()["code"] == "forbidden"
            assert write.status_code == 403
            assert write.get_json()["code"] == "forbidden"

    def test_configuration_change_is_audited(self, client, make_user, login):
        workspace, _owner, _contributor, _viewer, _project = _setup(make_user)
        login()
        client.put(
            f"/workspaces/api/workspaces/{workspace.id}/ip-allowlist",
            json={"enabled": True, "cidrs": ["10.0.0.0/8"]},
        )

        entry = (
            AuditLog.query.filter_by(workspace_id=workspace.id, action=audit.IP_ALLOWLIST_UPDATED)
            .order_by(AuditLog.id.desc())
            .first()
        )
        assert entry is not None
        assert entry.metadata_dict["enabled"] is True
        assert entry.metadata_dict["cidrs"] == ["10.0.0.0/8"]


class TestEnforcement:
    def test_allowed_ip_passes_and_blocked_ip_gets_structured_failure(
        self, client, make_user, login
    ):
        workspace, _owner, _contributor, _viewer, _project = _setup(make_user)
        login()
        client.put(
            f"/workspaces/api/workspaces/{workspace.id}/ip-allowlist",
            json={"enabled": True, "cidrs": ["10.0.0.0/8"]},
        )
        members_url = f"/workspaces/api/workspaces/{workspace.id}/members"

        allowed = _get(client, members_url, "10.1.2.3")
        blocked = _get(client, members_url, "127.0.0.1")
        html = _get(client, f"/workspaces/{workspace.id}/members", "127.0.0.1")

        assert allowed.status_code == 200
        assert blocked.status_code == 403
        assert blocked.get_json()["code"] == "forbidden"
        assert "not allowed" in blocked.get_json()["error"]
        assert html.status_code == 403
        assert html.mimetype == "text/html"

    def test_ipv6_ranges_are_matched_by_version(self, client, make_user, login):
        workspace, _owner, _contributor, _viewer, _project = _setup(make_user)
        login()
        client.put(
            f"/workspaces/api/workspaces/{workspace.id}/ip-allowlist",
            json={"enabled": True, "cidrs": ["2001:db8::/32"]},
        )
        url = f"/workspaces/api/workspaces/{workspace.id}/members"

        assert _get(client, url, "2001:db8::5").status_code == 200
        assert _get(client, url, "2001:db9::5").status_code == 403
        # An IPv4 client never matches an IPv6 range.
        assert _get(client, url, "10.0.0.1").status_code == 403

    def test_disabling_the_allowlist_restores_access(self, client, make_user, login):
        workspace, _owner, _contributor, _viewer, _project = _setup(make_user)
        login()
        url = f"/workspaces/api/workspaces/{workspace.id}/ip-allowlist"
        client.put(url, json={"enabled": True, "cidrs": ["10.0.0.0/8"]})
        assert _get(client, url, "127.0.0.1").status_code == 403

        disabled = _put(client, url, {"enabled": False}, "10.1.2.3")

        assert disabled.status_code == 200
        assert disabled.get_json()["enabled"] is False
        assert _get(client, url, "127.0.0.1").status_code == 200

    def test_project_scoped_routes_resolve_the_workspace(self, client, make_user, login):
        workspace, _owner, _contributor, _viewer, project = _setup(make_user)
        login()
        client.put(
            f"/workspaces/api/workspaces/{workspace.id}/ip-allowlist",
            json={"enabled": True, "cidrs": ["10.0.0.0/8"]},
        )

        blocked = _get(client, f"/workspaces/api/projects/{project.id}/tree", "127.0.0.1")
        allowed = _get(client, f"/workspaces/api/projects/{project.id}/tree", "10.1.2.3")

        assert blocked.status_code == 403
        assert blocked.get_json()["code"] == "forbidden"
        assert allowed.status_code == 200

    def test_health_and_static_assets_are_exempt(self, client, make_user, login):
        workspace, _owner, _contributor, _viewer, _project = _setup(make_user)
        login()
        client.put(
            f"/workspaces/api/workspaces/{workspace.id}/ip-allowlist",
            json={"enabled": True, "cidrs": ["10.0.0.0/8"]},
        )

        assert _get(client, "/health", "127.0.0.1").status_code == 200
        assert _get(client, "/static/css/style.css", "127.0.0.1").status_code == 200

    def test_enabled_config_with_no_rows_fails_closed(self, app, make_user):
        workspace, _owner, _contributor, _viewer, _project = _setup(make_user)
        row = ip_allowlist.set_config(workspace.id, enabled=False, cidrs=["10.0.0.0/8"])
        # Force the malformed state an operator could produce by hand-editing
        # the row; the guard must deny rather than allow.
        row.enabled = True
        row.cidrs = "[]"
        db.session.commit()

        assert ip_allowlist.is_allowed(workspace.id, "10.1.2.3") is False
        assert ip_allowlist.is_allowed(workspace.id, None) is False
