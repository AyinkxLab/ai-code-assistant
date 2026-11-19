"""Query-shape guards for the Phase 12 indexes (#697)."""

from __future__ import annotations

import contextlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from app.models import AuditLog, Conversation, Message, ProjectMessage

REPO_ROOT = Path(__file__).resolve().parent.parent

INDEXES = {
    "ix_conversations_user_updated": ("user_id", "updated_at"),
    "ix_conversations_workspace_updated": ("workspace_id", "updated_at"),
    "ix_messages_conversation_created": ("conversation_id", "created_at"),
    "ix_project_messages_session_created": ("session_id", "created_at"),
    "ix_audit_logs_user_created": ("user_id", "created_at"),
    "ix_audit_logs_action_created": ("action", "created_at"),
}

MODEL_TABLES = (Conversation, Message, ProjectMessage, AuditLog)


def _plan(db, query: str) -> str:
    rows = db.session.execute(text("EXPLAIN QUERY PLAN " + query)).all()
    return " | ".join(str(row[3]) for row in rows)


def test_models_declare_the_composite_indexes():
    declared = {
        index.name: tuple(column.name for column in index.columns)
        for model in MODEL_TABLES
        for index in model.__table__.indexes
    }
    for name, columns in INDEXES.items():
        assert declared.get(name) == columns, name


def test_conversation_list_uses_the_owner_updated_index(db):
    plan = _plan(
        db,
        "SELECT id FROM conversations WHERE user_id = 1 ORDER BY updated_at DESC",
    )
    assert "ix_conversations_user_updated" in plan, plan


def test_workspace_conversation_list_uses_the_workspace_updated_index(db):
    plan = _plan(
        db,
        "SELECT id FROM conversations WHERE workspace_id = 1 ORDER BY updated_at DESC",
    )
    assert "ix_conversations_workspace_updated" in plan, plan


def test_conversation_messages_use_the_conversation_created_index(db):
    plan = _plan(
        db,
        "SELECT id FROM messages WHERE conversation_id = 1 ORDER BY created_at",
    )
    assert "ix_messages_conversation_created" in plan, plan


def test_project_messages_use_the_session_created_index(db):
    plan = _plan(
        db,
        "SELECT id FROM project_messages WHERE session_id = 1 ORDER BY created_at",
    )
    assert "ix_project_messages_session_created" in plan, plan


def test_audit_filters_use_composite_indexes(db):
    by_user = _plan(
        db,
        "SELECT id FROM audit_logs WHERE user_id = 1 ORDER BY created_at DESC",
    )
    by_action = _plan(
        db,
        "SELECT id FROM audit_logs WHERE action = 'login' ORDER BY created_at DESC",
    )
    assert "ix_audit_logs_user_created" in by_user, by_user
    assert "ix_audit_logs_action_created" in by_action, by_action


@contextlib.contextmanager
def _migrated_sqlite():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{os.path.join(tmp, 'phase12.db')}"
        env = {
            **os.environ,
            "APP_ENV": "development",
            "SECRET_KEY": "phase12-migration-test",
            "DATABASE_URL": url,
        }
        result = subprocess.run(
            [sys.executable, "-m", "flask", "db", "upgrade"],
            cwd=REPO_ROOT,
            env=env,
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0, result.stderr
        yield url


def test_migration_creates_every_index():
    with _migrated_sqlite() as url:
        engine = create_engine(url)
        try:
            inspector = inspect(engine)
            for name in INDEXES:
                table = next(
                    table_name
                    for table_name in inspector.get_table_names()
                    if name in {index["name"] for index in inspector.get_indexes(table_name)}
                )
                assert table
        finally:
            engine.dispose()
