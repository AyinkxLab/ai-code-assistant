"""Organization custom roles and their admin surface (#597)."""

from __future__ import annotations

import os
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from alembic.config import Config
from alembic.script import ScriptDirectory

from app.extensions import db
from app.models import OrganizationRole, OrganizationRoleCapability, User, Workspace
from app.models.workspace_member import ROLE_VIEWER, WorkspaceMember
from app.services import permissions
from app.services.organization_roles import (
    BUILTIN_CAPABILITIES,
    OrganizationRoleError,
    capabilities_for_role,
    create_role,
    delete_role,
    ensure_builtin_roles,
    get_role,
    list_roles,
    update_role,
)

ROOT = Path(__file__).resolve().parents[1]


def _owner(username="owner", email="owner@example.com"):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _workspace(owner):
    workspace = Workspace(user_id=owner.id, name="Acme")
    db.session.add(workspace)
    db.session.commit()
    return workspace


class TestBuiltinRoles:
    def test_ensure_is_idempotent_and_seeds_all_builtins(self, db):
        owner = _owner()
        first = ensure_builtin_roles(owner.id)
        second = ensure_builtin_roles(owner.id)

        assert {role.name for role in first} == {"owner", "admin", "member"}
        assert [role.id for role in first] == [role.id for role in second]
        assert OrganizationRole.query.filter_by(owner_id=owner.id).count() == 3
        for name, capabilities in BUILTIN_CAPABILITIES.items():
            role = get_role(owner.id, next(r.id for r in first if r.name == name))
            assert role.is_builtin is True
            assert set(role.capability_names) == set(capabilities)
            assert role.capability_names == sorted(capabilities)

    def test_builtin_capabilities_are_plain_permission_actions(self, db):
        owner = _owner()
        ensure_builtin_roles(owner.id)
        for name, capabilities in BUILTIN_CAPABILITIES.items():
            assert set(capabilities) <= set(permissions.CAPABILITIES)
            assert set(capabilities_for_role(owner.id, name)) == set(capabilities)

    def test_ensure_does_not_duplicate_grants(self, db):
        owner = _owner()
        ensure_builtin_roles(owner.id)
        ensure_builtin_roles(owner.id)
        assert OrganizationRoleCapability.query.count() == sum(
            len(caps) for caps in BUILTIN_CAPABILITIES.values()
        )


class TestCustomRoles:
    def test_create_update_and_delete(self, db):
        owner = _owner()
        role = create_role(owner.id, "Auditor", ["view_members", "view_audit"])
        assert role.name == "auditor"
        assert role.to_dict()["capabilities"] == ["view_audit", "view_members"]

        updated = update_role(owner.id, role.id, name="reviewer", capabilities=["comment"])
        assert updated.name == "reviewer"
        assert updated.capability_names == ["comment"]

        delete_role(owner.id, role.id)
        with pytest.raises(OrganizationRoleError) as excinfo:
            get_role(owner.id, role.id)
        assert excinfo.value.code == "not_found"

    def test_duplicate_name_is_a_conflict(self, db):
        owner = _owner()
        create_role(owner.id, "auditor", ["view_members"])
        with pytest.raises(OrganizationRoleError) as excinfo:
            create_role(owner.id, "Auditor", ["comment"])
        assert excinfo.value.code == "conflict"

    def test_names_are_scoped_per_owner(self, db):
        first = _owner("first", "first@example.com")
        second = _owner("second", "second@example.com")
        create_role(first.id, "auditor", ["view_members"])
        role = create_role(second.id, "auditor", ["comment"])
        assert role.owner_id == second.id

    @pytest.mark.parametrize(
        "name",
        ["", "  ", "1auditor", "bad name", "x" * 65, "../escape"],
    )
    def test_invalid_names_return_invalid_input(self, db, name):
        owner = _owner()
        with pytest.raises(OrganizationRoleError) as excinfo:
            create_role(owner.id, name, ["view_members"])
        assert excinfo.value.code == "invalid_input"

    def test_unknown_and_empty_capabilities_return_invalid_input(self, db):
        owner = _owner()
        for capabilities in (["delete_everything"], [], "  ", None):
            with pytest.raises(OrganizationRoleError) as excinfo:
                create_role(owner.id, "auditor", capabilities)
            assert excinfo.value.code == "invalid_input"

    def test_builtins_cannot_be_renamed_or_deleted(self, db):
        owner = _owner()
        builtin = next(role for role in list_roles(owner.id) if role.name == "owner")
        with pytest.raises(OrganizationRoleError) as excinfo:
            update_role(owner.id, builtin.id, name="superowner")
        assert excinfo.value.code == "conflict"
        with pytest.raises(OrganizationRoleError) as excinfo:
            delete_role(owner.id, builtin.id)
        assert excinfo.value.code == "conflict"

    def test_other_owners_roles_are_not_visible(self, db):
        owner = _owner()
        outsider = _owner("outsider", "outsider@example.com")
        role = create_role(owner.id, "auditor", ["view_members"])
        with pytest.raises(OrganizationRoleError) as excinfo:
            update_role(outsider.id, role.id, name="renamed")
        assert excinfo.value.code == "not_found"


class TestAdminRoutes:
    def test_owner_can_list_and_create_roles(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()

        listed = client.get(f"/admin/organizations/{workspace.id}/roles")
        assert listed.status_code == 200
        names = {role["name"] for role in listed.get_json()["roles"]}
        assert {"owner", "admin", "member"} <= names
        assert "view_members" in listed.get_json()["capabilities"]

        created = client.post(
            f"/admin/organizations/{workspace.id}/roles",
            json={"name": "Auditor", "capabilities": ["view_members", "view_audit"]},
        )
        assert created.status_code == 201
        assert created.get_json()["role"]["capabilities"] == ["view_audit", "view_members"]

    def test_non_admin_is_forbidden(self, client, make_user, login, db):
        owner = make_user()
        member = make_user(username="member", email="member@example.com")
        workspace = _workspace(owner)
        db.session.add(
            WorkspaceMember(workspace_id=workspace.id, user_id=member.id, role=ROLE_VIEWER)
        )
        db.session.commit()
        login(email="member@example.com")

        response = client.post(
            f"/admin/organizations/{workspace.id}/roles",
            json={"name": "auditor", "capabilities": ["view_members"]},
        )
        assert response.status_code == 403
        assert response.get_json()["code"] == "forbidden"

    def test_invalid_input_conflicts_and_not_found_are_structured(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()
        base = f"/admin/organizations/{workspace.id}/roles"

        invalid = client.post(base, json={"name": "auditor", "capabilities": ["nope"]})
        assert invalid.status_code == 400
        assert invalid.get_json()["code"] == "invalid_input"

        created = client.post(base, json={"name": "auditor", "capabilities": ["view_members"]})
        assert created.status_code == 201
        role_id = created.get_json()["role"]["id"]
        duplicate = client.post(base, json={"name": "auditor", "capabilities": ["comment"]})
        assert duplicate.status_code == 409
        assert duplicate.get_json()["code"] == "conflict"

        missing = client.patch(f"{base}/999999", json={"name": "renamed"})
        assert missing.status_code == 404
        assert missing.get_json()["code"] == "not_found"

        builtin = next(
            role for role in client.get(base).get_json()["roles"] if role["name"] == "owner"
        )
        refused = client.delete(f"{base}/{builtin['id']}")
        assert refused.status_code == 409
        assert refused.get_json()["code"] == "conflict"

        deleted = client.delete(f"{base}/{role_id}")
        assert deleted.status_code == 200
        assert deleted.get_json() == {"ok": True}

    def test_update_route_replaces_capabilities(self, client, make_user, login):
        owner = make_user()
        workspace = _workspace(owner)
        login()
        base = f"/admin/organizations/{workspace.id}/roles"
        role_id = client.post(
            base, json={"name": "auditor", "capabilities": ["view_members"]}
        ).get_json()["role"]["id"]
        response = client.patch(
            f"{base}/{role_id}", json={"name": "reviewer", "capabilities": ["comment"]}
        )
        assert response.status_code == 200
        assert response.get_json()["role"]["capabilities"] == ["comment"]


class TestMigration:
    def test_single_head_chains_from_the_plugin_governance_migration(self):
        config = Config()
        config.set_main_option("script_location", str(ROOT / "migrations"))
        script = ScriptDirectory.from_config(config)
        heads = script.get_heads()
        assert len(heads) == 1
        revision = script.get_revision(heads[0])
        ancestry = []
        while revision is not None:
            ancestry.append(revision.revision)
            down = revision.down_revision
            if isinstance(down, tuple):  # historical merge revision
                down = down[0]
            revision = script.get_revision(down) if down else None
        assert "c5d6e7f8a9b0" in ancestry
        assert script.get_revision("c5d6e7f8a9b0").down_revision == "b4e2f1a3c5d7"

    def test_upgrade_backfills_builtin_roles(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "roles.db"
            env = {
                **os.environ,
                "APP_ENV": "development",
                "SECRET_KEY": "migration-test-secret",
                "DATABASE_URL": f"sqlite:///{path}",
            }

            def flask_db(*args):
                return subprocess.run(
                    [sys.executable, "-m", "flask", "db", *args],
                    cwd=ROOT,
                    env=env,
                    capture_output=True,
                    text=True,
                )

            # Upgrade to the parent revision first so the users table exists,
            # then insert an owner before the backfill migration runs.
            parent = flask_db("upgrade", "b4e2f1a3c5d7")
            assert parent.returncode == 0, parent.stderr
            connection = sqlite3.connect(path)
            try:
                connection.execute(
                    "INSERT INTO users (id, username, email, password_hash, is_active, "
                    "is_admin, created_at, updated_at) VALUES "
                    "(1, 'owner', 'owner@example.com', 'hash', 1, 0, '2026-01-01', '2026-01-01')"
                )
                connection.commit()
            finally:
                connection.close()

            result = flask_db("upgrade")
            assert result.returncode == 0, result.stderr

            connection = sqlite3.connect(path)
            try:
                names = {
                    row[0]
                    for row in connection.execute(
                        "SELECT name FROM organization_roles WHERE owner_id = 1"
                    )
                }
                grants = connection.execute(
                    "SELECT r.name, c.capability FROM organization_roles r "
                    "JOIN organization_role_capabilities c ON c.role_id = r.id "
                    "WHERE r.owner_id = 1"
                ).fetchall()
            finally:
                connection.close()
            assert names == {"owner", "admin", "member"}
            assert ("owner", "manage_members") in grants
            assert ("member", "leave_workspace") in grants
