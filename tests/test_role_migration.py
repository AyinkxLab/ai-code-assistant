"""Tests for the Phase 11 role migration and default-role backfill (#605)."""

from __future__ import annotations

import importlib.util
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from app.extensions import db
from app.models import OrganizationRole, RoleMigrationRecord, User, Workspace, WorkspaceMember
from app.models.workspace_member import STATUS_ACTIVE, STATUS_REMOVED
from app.services import role_migration
from scripts import role_migration as role_migration_cli

REPO_ROOT = Path(__file__).resolve().parents[1]
MIGRATION_PATH = REPO_ROOT / "migrations" / "versions" / "d6e7f8a9b0c1_add_organization_roles.py"


def _owner(username="alice", email="alice@example.com"):
    user = User(username=username, email=email)
    user.set_password("supersecret123")
    db.session.add(user)
    db.session.commit()
    return user


def _workspace(owner, name="Acme"):
    workspace = Workspace(user_id=owner.id, name=name)
    db.session.add(workspace)
    db.session.commit()
    return workspace


def _member(workspace, user, role, status=STATUS_ACTIVE):
    member = WorkspaceMember(workspace_id=workspace.id, user_id=user.id, role=role, status=status)
    db.session.add(member)
    db.session.commit()
    return member


def _allow_duplicate_memberships():
    """Recreate the SQLite table without its unique (workspace, user) index."""
    db.session.execute(text("ALTER TABLE workspace_members RENAME TO workspace_members_old"))
    db.session.execute(
        text(
            "CREATE TABLE workspace_members ("
            "id INTEGER PRIMARY KEY, workspace_id INTEGER NOT NULL, "
            "user_id INTEGER NOT NULL, role VARCHAR(20) NOT NULL, "
            "status VARCHAR(20) NOT NULL, joined_at DATETIME, removed_at DATETIME, "
            "last_seen_at DATETIME, created_at DATETIME, updated_at DATETIME)"
        )
    )
    db.session.execute(
        text(
            "INSERT INTO workspace_members SELECT id, workspace_id, user_id, role, status, "
            "joined_at, removed_at, last_seen_at, created_at, updated_at "
            "FROM workspace_members_old"
        )
    )
    db.session.execute(text("DROP TABLE workspace_members_old"))
    db.session.commit()


class TestDryRun:
    def test_plan_reports_alias_without_writing(self, app):
        owner = _owner()
        workspace = _workspace(owner)
        member = _member(workspace, owner, "reader")

        result = role_migration.plan_role_migration()

        assert result["scanned_workspaces"] == 1
        assert len(result["role_changes"]) == 1
        assert result["role_changes"][0]["after"]["role"] == "viewer"
        assert len(result["default_roles_to_create"]) == 4
        assert member.role == "reader"
        assert OrganizationRole.query.count() == 0
        assert RoleMigrationRecord.query.count() == 0
        text_report = role_migration.format_plan(result)
        assert "Role migration (dry run)" in text_report
        assert "No changes written" in text_report

    def test_unknown_legacy_role_is_skipped_not_promoted(self, app):
        owner = _owner()
        other = _owner("bob", "bob@example.com")
        workspace = _workspace(owner)
        member = _member(workspace, other, "wizard")

        result = role_migration.plan_role_migration()

        assert result["role_changes"] == []
        assert result["unknown"][0]["reason"] == "unknown_legacy_role"
        assert result["skipped"][0]["reason"] == "no known role to migrate"
        assert member.role == "wizard"

    def test_duplicates_choose_known_active_highest_role(self, app):
        _allow_duplicate_memberships()
        owner = _owner()
        other = _owner("bob", "bob@example.com")
        workspace = _workspace(owner)
        first = _member(workspace, other, "administrator")
        second = _member(workspace, other, "viewer")

        result = role_migration.plan_role_migration()

        assert result["duplicates"][0]["keep_role"] == "admin"
        by_id = {change["row_id"]: change["after"] for change in result["role_changes"]}
        assert by_id[first.id]["role"] == "admin"
        assert by_id[first.id]["status"] == STATUS_ACTIVE
        assert by_id[second.id]["status"] == STATUS_REMOVED

    def test_unknown_active_row_is_never_promoted_by_duplicate_resolution(self, app):
        _allow_duplicate_memberships()
        owner = _owner()
        other = _owner("bob", "bob@example.com")
        workspace = _workspace(owner)
        unknown = _member(workspace, other, "wizard")
        known = _member(workspace, other, "owner", status=STATUS_REMOVED)

        result = role_migration.plan_role_migration()

        by_id = {change["row_id"]: change["after"] for change in result["role_changes"]}
        assert by_id[known.id]["role"] == "owner"
        assert by_id[known.id]["status"] == STATUS_ACTIVE
        assert by_id[unknown.id]["status"] == STATUS_REMOVED
        assert by_id[unknown.id]["role"] == "wizard"


class TestApplyVerifyRollback:
    def test_apply_is_idempotent_and_verify_passes(self, app):
        owner = _owner()
        workspace = _workspace(owner)
        member = _member(workspace, owner, "editor")

        first = role_migration.apply_role_migration()
        second = role_migration.apply_role_migration()

        assert len(first["applied_changes"]) == 1
        assert second["applied_changes"] == []
        assert len(first["created_roles"]) == 4
        assert second["created_roles"] == []
        db.session.refresh(member)
        assert member.role == "contributor"
        assert OrganizationRole.query.count() == 4
        assert role_migration.verify_role_migration()["ok"] is True
        text_report = role_migration.format_verify(role_migration.verify_role_migration())
        assert "verification passed" in text_report

    def test_rollback_restores_roles_and_removes_backfilled_roles(self, app):
        owner = _owner()
        other = _owner("bob", "bob@example.com")
        workspace = _workspace(owner)
        member = _member(workspace, other, "reader")
        role_migration.apply_role_migration()

        result = role_migration.rollback_role_migration()

        assert result["records"] == 2
        assert len(result["restored"]) == 1
        assert len(result["deleted_roles"]) == 4
        assert result["deleted_roles"] == sorted(result["deleted_roles"])
        db.session.refresh(member)
        assert member.role == "reader"
        assert OrganizationRole.query.count() == 0
        assert RoleMigrationRecord.query.count() == 0
        assert role_migration.verify_role_migration()["ok"] is False

    def test_owner_filter_limits_the_plan(self, app):
        alice = _owner()
        bob = _owner("bob", "bob@example.com")
        _workspace(alice)
        _workspace(bob)

        result = role_migration.plan_role_migration(owner_id=alice.id)

        assert result["scanned_workspaces"] == 1
        assert [item["owner_id"] for item in result["default_roles_to_create"]] == [alice.id] * 4


class TestStructuredErrors:
    def test_invalid_owner_id_has_a_structured_error(self):
        with pytest.raises(role_migration.RoleMigrationError) as excinfo:
            role_migration.parse_owner_id("nope")

        assert excinfo.value.code == "invalid_owner_id"
        assert excinfo.value.to_dict()["error"]["details"] == {"owner_id": "nope"}

    def test_cli_reports_structured_errors(self, capsys):
        assert role_migration_cli.main(["--owner-id", "nope"]) == 2

        payload = json.loads(capsys.readouterr().err)
        assert payload["error"]["code"] == "invalid_owner_id"
        assert payload["error"]["message"]

    def test_cli_dry_run_writes_nothing(self, app, capsys):
        _workspace(_owner())

        assert role_migration_cli.main([], app=app) == 0

        output = capsys.readouterr().out
        assert "No changes written" in output
        assert OrganizationRole.query.count() == 0


class TestSchemaMigration:
    def test_migration_constants_match_the_service(self):
        spec = importlib.util.spec_from_file_location("role_migration_revision", MIGRATION_PATH)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        assert module.revision == "d6e7f8a9b0c1"
        assert module.down_revision == "b4e2f1a3c5d7"
        assert {
            name: list(capabilities)
            for name, capabilities in role_migration.BUILTIN_ROLE_CAPABILITIES.items()
        } == module.BUILTIN_ROLE_CAPABILITIES

    def test_upgrade_creates_tables_and_backfills_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            db_url = f"sqlite:///{os.path.join(tmp, 'roles.db')}"
            env = {
                **os.environ,
                "APP_ENV": "development",
                "SECRET_KEY": "roles-migration-test",
                "DATABASE_URL": db_url,
            }
            upgrade = subprocess.run(
                [sys.executable, "-m", "flask", "--app", "app", "db", "upgrade"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            assert upgrade.returncode == 0, upgrade.stderr

            engine = create_engine(db_url)
            inspector = inspect(engine)
            assert {"organization_roles", "role_migration_records"} <= set(
                inspector.get_table_names()
            )
            with engine.connect() as connection:
                # No workspaces exist in a freshly migrated database, so no
                # owner rows are seeded. The script backfills later owners.
                count = connection.execute(
                    text("SELECT COUNT(*) FROM organization_roles")
                ).scalar_one()
            assert count == 0
            engine.dispose()

            downgrade = subprocess.run(
                [sys.executable, "-m", "flask", "--app", "app", "db", "downgrade", "base"],
                cwd=REPO_ROOT,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            assert downgrade.returncode == 0, downgrade.stderr
