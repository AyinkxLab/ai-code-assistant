"""Migration integrity tests.

Verifies that a clean ``flask db upgrade`` from the base revision produces a
schema that matches the models, and that ``flask db downgrade base`` removes
the Phase 8 plugin tables. Runs against a temporary SQLite file.
"""

import contextlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parent.parent

PLUGIN_TABLES = {"plugins", "plugin_installations", "plugin_capability_grants"}

# Column set per plugin table as defined by the models.
EXPECTED_COLUMNS = {
    "plugins": {
        "id",
        "name",
        "version",
        "description",
        "author",
        "entry_point",
        "capabilities",
        "permissions",
        "dependencies",
        "configuration",
        "enabled",
        "installed_at",
        "updated_at",
    },
    "plugin_installations": {
        "id",
        "plugin_id",
        "workspace_id",
        "enabled",
        "granted_capabilities",
        "config",
        "installed_at",
        "updated_at",
        "installed_by_id",
    },
    "plugin_capability_grants": {
        "id",
        "plugin_id",
        "workspace_id",
        "capability",
        "granted_at",
        "granted_by_id",
    },
}


@contextlib.contextmanager
def _migration_db():
    """Upgrade a temp DB to head, yield its URL, then downgrade to base."""
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite:///{os.path.join(tmp, 'mig.db')}"
        result = _run_flask(["db", "upgrade"], {"DATABASE_URL": db_url})
        assert result.returncode == 0, result.stderr
        yield db_url
        down = _run_flask(["db", "downgrade", "base"], {"DATABASE_URL": db_url})
        assert down.returncode == 0, down.stderr


def _run_flask(args, env):
    env = {**os.environ, **env, "APP_ENV": "development", "SECRET_KEY": "migration-test-secret"}
    return subprocess.run(
        [sys.executable, "-m", "flask", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


@contextlib.contextmanager
def _inspect(url):
    engine = create_engine(url)
    try:
        yield inspect(engine)
    finally:
        engine.dispose()


class TestMigrationUpgrade:
    def test_plugin_tables_exist(self):
        with _migration_db() as db_url, _inspect(db_url) as insp:
            tables = set(insp.get_table_names())
            assert PLUGIN_TABLES.issubset(tables)

    def test_plugin_table_columns_match_models(self):
        with _migration_db() as db_url, _inspect(db_url) as insp:
            for table, expected in EXPECTED_COLUMNS.items():
                actual = {col["name"] for col in insp.get_columns(table)}
                assert actual == expected, f"{table} columns mismatch: {actual ^ expected}"

    def test_upgrade_then_upgrade_is_noop(self):
        with _migration_db() as db_url:
            result = _run_flask(["db", "upgrade"], {"DATABASE_URL": db_url})
            assert result.returncode == 0, result.stderr
            with _inspect(db_url) as insp:
                assert PLUGIN_TABLES.issubset(set(insp.get_table_names()))

    def test_downgrade_removes_plugin_tables(self):
        with _migration_db() as db_url:
            result = _run_flask(["db", "downgrade", "base"], {"DATABASE_URL": db_url})
            assert result.returncode == 0, result.stderr
            with _inspect(db_url) as insp:
                tables = set(insp.get_table_names())
                assert PLUGIN_TABLES.isdisjoint(tables), (
                    "plugin tables still present after downgrade"
                )


class TestMigrationHead:
    def test_head_is_phase8(self):
        result = _run_flask(["db", "heads"], {"DATABASE_URL": "sqlite:///:memory:"})
        assert result.returncode == 0, result.stderr
        assert "b3c2d1a0f9e8" in (result.stdout + result.stderr)
