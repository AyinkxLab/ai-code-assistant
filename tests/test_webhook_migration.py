"""Migration coverage for the webhook subscription extensions (#667)."""

from __future__ import annotations

import os
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, inspect

REPO_ROOT = Path(__file__).resolve().parent.parent


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


def test_webhook_extension_upgrade_and_downgrade():
    with tempfile.TemporaryDirectory() as tmp:
        db_url = f"sqlite:///{os.path.join(tmp, 'webhook_mig.db')}"
        up = _run_flask(["db", "upgrade"], db_url)
        assert up.returncode == 0, up.stderr

        engine = create_engine(db_url)
        try:
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            assert {"webhook_subscriptions", "webhook_subscription_events"} <= tables

            subscription_columns = {
                column["name"] for column in inspector.get_columns("webhook_subscriptions")
            }
            assert {
                "id",
                "workspace_id",
                "url",
                "event_type",
                "secret",
                "secret_hash",
                "is_active",
                "created_at",
                "updated_at",
            } <= subscription_columns
            event_columns = {
                column["name"] for column in inspector.get_columns("webhook_subscription_events")
            }
            assert event_columns == {"id", "subscription_id", "event_type"}

            subscription_indexes = {
                index["name"] for index in inspector.get_indexes("webhook_subscriptions")
            }
            assert "ix_webhook_subscriptions_active" in subscription_indexes

            event_indexes = {
                index["name"] for index in inspector.get_indexes("webhook_subscription_events")
            }
            assert {
                "ix_webhook_subscription_events_subscription_id",
                "ix_webhook_subscription_events_event_type",
                "ix_webhook_subscription_events_type_subscription",
            } <= event_indexes
        finally:
            engine.dispose()

        down = _run_flask(["db", "downgrade", "f2a3b4c5d6e7"], db_url)
        assert down.returncode == 0, down.stderr
        engine = create_engine(db_url)
        try:
            inspector = inspect(engine)
            tables = set(inspector.get_table_names())
            assert "webhook_subscription_events" not in tables
            columns = {column["name"] for column in inspector.get_columns("webhook_subscriptions")}
            assert "secret_hash" not in columns
            assert "webhook_subscriptions" in tables
        finally:
            engine.dispose()
