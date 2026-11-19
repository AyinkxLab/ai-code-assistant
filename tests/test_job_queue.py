"""Durable background job queue: enqueue, retry, exhaustion, CLI (#699)."""

from __future__ import annotations

import contextlib
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect

from app.models import Job
from app.models.job import STATUS_FAILED, STATUS_QUEUED, STATUS_SUCCEEDED
from app.services import job_queue

REPO_ROOT = Path(__file__).resolve().parent.parent

SEEN: list[dict] = []


@job_queue.register_handler("test.success")
def _success(payload):
    SEEN.append(payload)
    return {"ok": True}


@job_queue.register_handler("test.fail")
def _fail(payload):
    raise RuntimeError("handler exploded")


def test_enqueue_rejects_invalid_input(app):
    with pytest.raises(job_queue.JobQueueError):
        job_queue.enqueue("not.registered", {})
    with pytest.raises(job_queue.JobQueueError):
        job_queue.enqueue("test.success", ["not", "an", "object"])
    app.config["JOB_QUEUE_MAX_PAYLOAD_BYTES"] = 5
    with pytest.raises(job_queue.JobQueueError):
        job_queue.enqueue("test.success", {"too": "large"})
    with pytest.raises(job_queue.JobQueueError):
        job_queue.enqueue("test.success", {}, max_attempts=0)


def test_disabled_queue_refuses_new_jobs(app):
    app.config["JOB_QUEUE_ENABLED"] = False
    with pytest.raises(job_queue.JobQueueError):
        job_queue.enqueue("test.success", {})


def test_successful_job_runs_once(app, db):
    SEEN.clear()
    job = job_queue.enqueue("test.success", {"value": 1})
    assert job.status == STATUS_QUEUED

    processed = job_queue.run_due(limit=10)

    db.session.refresh(job)
    assert processed == {"processed": 1, "succeeded": 1, "retried": 0, "failed": 0}
    assert job.status == STATUS_SUCCEEDED
    assert job.attempts == 1
    assert job.finished_at is not None
    assert SEEN == [{"value": 1}]


def test_failure_retries_with_backoff_then_exhausts(app, db, monkeypatch):
    app.config["JOB_QUEUE_RETRY_BASE_SECONDS"] = 10
    app.config["JOB_QUEUE_RETRY_MAX_SECONDS"] = 60
    delays: list[float] = []
    monkeypatch.setattr(
        job_queue, "retry_delay_seconds", lambda attempts: delays.append(attempts) or 0
    )

    job = job_queue.enqueue("test.fail", {}, max_attempts=2)
    job_queue.run_due(limit=1)

    db.session.refresh(job)
    assert job.status == STATUS_QUEUED
    assert job.attempts == 1
    assert "handler exploded" in job.last_error

    job_queue.run_due()

    db.session.refresh(job)
    assert job.status == STATUS_FAILED
    assert job.attempts == 2
    assert delays == [1]


def test_retry_delay_is_exponential_and_capped(app):
    app.config["JOB_QUEUE_RETRY_BASE_SECONDS"] = 10
    app.config["JOB_QUEUE_RETRY_MAX_SECONDS"] = 40
    assert job_queue.retry_delay_seconds(1) == 10
    assert job_queue.retry_delay_seconds(2) == 20
    assert job_queue.retry_delay_seconds(3) == 40
    assert job_queue.retry_delay_seconds(4) == 40


def test_webhook_url_validation_rejects_private_and_non_http_targets():
    with pytest.raises(job_queue.JobQueueError):
        job_queue.validate_webhook_url("ftp://example.com/hook")
    with pytest.raises(job_queue.JobQueueError):
        job_queue.validate_webhook_url("http://127.0.0.1/hook")
    with pytest.raises(job_queue.JobQueueError):
        job_queue.validate_webhook_url("")


def test_cli_worker_and_list(app, db):
    SEEN.clear()
    job = job_queue.enqueue("test.success", {"via": "cli"})
    runner = app.test_cli_runner()

    worked = runner.invoke(args=["jobs", "worker", "--once"])
    assert worked.exit_code == 0, worked.output

    db.session.refresh(job)
    assert job.status == STATUS_SUCCEEDED

    listed = runner.invoke(args=["jobs", "list", "--json"])
    assert listed.exit_code == 0, listed.output
    rows = json.loads(listed.output)
    assert rows[0]["id"] == job.id
    assert "payload" not in rows[0]


@contextlib.contextmanager
def _migrated_sqlite():
    with tempfile.TemporaryDirectory() as tmp:
        url = f"sqlite:///{os.path.join(tmp, 'jobs.db')}"
        env = {
            **os.environ,
            "APP_ENV": "development",
            "SECRET_KEY": "jobs-migration-test",
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


def test_migration_creates_the_jobs_table_and_indexes():
    with _migrated_sqlite() as url:
        engine = create_engine(url)
        try:
            inspector = inspect(engine)
            assert "jobs" in inspector.get_table_names()
            indexes = {index["name"] for index in inspector.get_indexes("jobs")}
            assert {"ix_jobs_status_available", "ix_jobs_kind_status"} <= indexes
        finally:
            engine.dispose()


def test_model_declares_job_indexes():
    declared = {index.name for index in Job.__table__.indexes}
    assert {"ix_jobs_status_available", "ix_jobs_kind_status"} <= declared
