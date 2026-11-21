"""Upgrade/downgrade test for the integration health migration (#684)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parent.parent
PREVIOUS_REVISION = "e4f5a6b7c8d9"

HEALTH_COLUMNS = {
    "id",
    "workspace_id",
    "integration",
    "status",
    "last_success_at",
    "last_error_at",
    "last_error",
    "last_error_code",
    "last_status_code",
    "last_checked_at",
    "created_at",
    "updated_at",
}
SUPPRESSION_COLUMNS = {
    "id",
    "workspace_id",
    "email",
    "reason",
    "created_at",
}


def _run_flask(args, db_url):
    env = {
        **os.environ,
        "DATABASE_URL": db_url,
        "APP_ENV": "development",
        "SECRET_KEY": "migration-test-secret",
    }
    return subprocess.run(
        [sys.executable, "-m", "flask", *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def test_integration_tables_upgrade_and_downgrade():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite:///{os.path.join(tmp, 'integration_health.db')}"

        up = _run_flask(["db", "upgrade"], db_url)
        assert up.returncode == 0, up.stderr

        engine = create_engine(db_url)
        try:
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            assert {"integration_health", "email_suppressions"} <= tables
            assert {
                column["name"] for column in inspector.get_columns("integration_health")
            } == HEALTH_COLUMNS
            assert {
                column["name"] for column in inspector.get_columns("email_suppressions")
            } == SUPPRESSION_COLUMNS
        finally:
            engine.dispose()

        down = _run_flask(["db", "downgrade", PREVIOUS_REVISION], db_url)
        assert down.returncode == 0, down.stderr

        engine = create_engine(db_url)
        try:
            tables = set(inspect(engine).get_table_names())
            assert "integration_health" not in tables
            assert "email_suppressions" not in tables
        finally:
            engine.dispose()
