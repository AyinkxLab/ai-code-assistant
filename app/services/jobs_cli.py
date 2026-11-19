"""Operator CLI for the durable background job queue (#699).

Usage::

    flask jobs worker [--once] [--max-jobs N] [--poll-interval S]
    flask jobs list [--status STATUS] [--limit N] [--json]

The worker processes due jobs in bounded batches and exits after ``--max-jobs``
or when ``--once`` is set. Run it as a separate process from the web server.
"""

from __future__ import annotations

import json
import time

import click
from flask import Flask, current_app

from app.models.job import STATUS_FAILED, STATUS_QUEUED, STATUS_RUNNING, STATUS_SUCCEEDED

EXIT_OK = 0
VALID_STATUSES = (STATUS_QUEUED, STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_FAILED)


def register_jobs_cli(app: Flask) -> None:
    """Register the ``jobs`` command group on ``app``."""

    @app.cli.group("jobs")
    def jobs_group():
        """Background job queue commands."""

    @jobs_group.command("worker")
    @click.option("--once", is_flag=True, help="Drain the current due jobs and exit.")
    @click.option(
        "--max-jobs",
        type=click.IntRange(min=1, max=1_000_000),
        default=None,
        help="Stop after this many processed jobs.",
    )
    @click.option(
        "--poll-interval",
        type=click.FloatRange(min=0),
        default=None,
        help="Seconds to sleep when the queue is empty (default: config value).",
    )
    def worker(once: bool, max_jobs: int | None, poll_interval: float | None) -> int:
        """Work due jobs until stopped, --once, or --max-jobs."""
        from app.services.job_queue import run_due

        batch_size = int(current_app.config.get("JOB_QUEUE_BATCH_SIZE", 50))
        sleep_for = (
            poll_interval
            if poll_interval is not None
            else float(current_app.config.get("JOB_QUEUE_WORKER_POLL_SECONDS", 5))
        )
        processed = 0
        totals = {"succeeded": 0, "retried": 0, "failed": 0}
        while True:
            remaining = None if max_jobs is None else max_jobs - processed
            if remaining is not None and remaining <= 0:
                break
            limit = batch_size if remaining is None else min(batch_size, remaining)
            counts = run_due(limit=limit)
            processed += counts["processed"]
            for key in totals:
                totals[key] += counts[key]
            if once:
                break
            if remaining is not None and processed >= max_jobs:
                break
            if counts["processed"] == 0:
                time.sleep(max(0.0, sleep_for))

        click.echo(
            f"Worker stopped: processed={processed} "
            f"succeeded={totals['succeeded']} retried={totals['retried']} "
            f"failed={totals['failed']}"
        )
        return EXIT_OK

    @jobs_group.command("list")
    @click.option(
        "--status",
        type=click.Choice(VALID_STATUSES),
        default=None,
        help="Only jobs with this status.",
    )
    @click.option(
        "--limit",
        type=click.IntRange(min=1, max=500),
        default=50,
        show_default=True,
        help="Maximum jobs to show.",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def list_jobs_command(status: str | None, limit: int, as_json: bool) -> int:
        """List recent jobs (newest first); payloads are never printed."""
        from app.services.job_queue import list_jobs

        rows = [job.to_dict() for job in list_jobs(status=status, limit=limit)]
        if as_json:
            click.echo(json.dumps(rows, indent=2, sort_keys=True))
            return EXIT_OK
        for row in rows:
            click.echo(
                f"{row['id']} {row['kind']} {row['status']} "
                f"attempts={row['attempts']}/{row['max_attempts']} "
                f"available_at={row['available_at']}"
            )
        return EXIT_OK


__all__ = ["EXIT_OK", "register_jobs_cli"]
