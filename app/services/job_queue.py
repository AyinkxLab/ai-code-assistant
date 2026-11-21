"""Durable, bounded background job queue (#699).

Long-running work (project exports, project scans, webhook deliveries) is
persisted as a :class:`~app.models.job.Job` row instead of running in the
request. A worker claims due jobs, runs the handler registered for the job kind,
and retries failures with exponential backoff until ``max_attempts`` is
exhausted. Exhausted jobs stay ``failed`` with the last error for operators.

The queue is bounded at every edge:

* payloads are JSON objects capped by ``JOB_QUEUE_MAX_PAYLOAD_BYTES``;
* one ``run_due`` call processes at most ``JOB_QUEUE_BATCH_SIZE`` rows;
* retry delays are capped by ``JOB_QUEUE_RETRY_MAX_SECONDS``;
* webhook deliveries only accept public HTTP(S) targets and do not follow
  redirects.

Use the worker CLI:

.. code-block:: bash

    flask jobs worker --once
    flask jobs worker --max-jobs 500
    flask jobs list --status failed --json
"""

from __future__ import annotations

import ipaddress
import json
import logging
import socket
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from urllib.parse import urlparse

import requests
from flask import current_app

from app.extensions import db
from app.models import Job, Project, ProjectFile, WebhookSubscription
from app.models.job import (
    STATUS_FAILED,
    STATUS_QUEUED,
    STATUS_RUNNING,
    STATUS_SUCCEEDED,
)
from app.observability.metrics import JOBS_QUEUE_DEPTH, WEBHOOK_DELIVERIES
from app.services import webhook_signing

logger = logging.getLogger(__name__)

DEFAULT_MAX_ATTEMPTS = 5
DEFAULT_RETRY_BASE_SECONDS = 10.0
DEFAULT_RETRY_MAX_SECONDS = 3600.0
DEFAULT_BATCH_SIZE = 50
DEFAULT_MAX_PAYLOAD_BYTES = 16 * 1024

_HANDLERS: dict[str, Callable] = {}


class JobQueueError(ValueError):
    """Raised when a job cannot be enqueued or found."""


def _config(name: str, default):
    try:
        return current_app.config.get(name, default)
    except RuntimeError:  # no application context (unit tests/helpers)
        return default


def register_handler(kind: str):
    """Register ``handler(payload)`` for ``kind``; usable as a decorator."""

    def decorator(handler):
        _HANDLERS[kind] = handler
        return handler

    return decorator


def get_handler(kind: str):
    return _HANDLERS.get(kind)


def registered_kinds() -> tuple[str, ...]:
    return tuple(sorted(_HANDLERS))


def _now() -> datetime:
    return datetime.now(UTC)


def _encode_payload(payload) -> str:
    if not isinstance(payload, dict):
        raise JobQueueError("payload must be a JSON object")
    try:
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":"), default=str)
    except (TypeError, ValueError) as exc:
        raise JobQueueError("payload must be JSON-serializable") from exc
    limit = int(_config("JOB_QUEUE_MAX_PAYLOAD_BYTES", DEFAULT_MAX_PAYLOAD_BYTES))
    if len(encoded.encode("utf-8")) > limit:
        raise JobQueueError(f"payload exceeds {limit} bytes")
    return encoded


def _decode_payload(raw: str) -> dict:
    try:
        value = json.loads(raw or "{}")
    except (TypeError, ValueError) as exc:
        raise JobQueueError("stored payload is not valid JSON") from exc
    if not isinstance(value, dict):
        raise JobQueueError("stored payload must be a JSON object")
    return value


def retry_delay_seconds(attempts: int) -> float:
    """Exponential backoff: base * 2^(attempts-1), capped."""
    base = float(_config("JOB_QUEUE_RETRY_BASE_SECONDS", DEFAULT_RETRY_BASE_SECONDS))
    maximum = float(_config("JOB_QUEUE_RETRY_MAX_SECONDS", DEFAULT_RETRY_MAX_SECONDS))
    return min(max(0.0, base) * (2 ** max(0, attempts - 1)), max(0.0, maximum))


def enqueue(kind: str, payload: dict | None = None, *, max_attempts: int | None = None) -> Job:
    """Persist a due job or raise :class:`JobQueueError` for invalid input."""
    kind = (kind or "").strip()
    if not bool(_config("JOB_QUEUE_ENABLED", True)):
        raise JobQueueError("job queue is disabled")
    if not kind or len(kind) > 50:
        raise JobQueueError("kind must be 1-50 characters")
    if kind not in _HANDLERS:
        raise JobQueueError(f"no handler registered for {kind!r}")

    limit = int(_config("JOB_QUEUE_MAX_ATTEMPTS", DEFAULT_MAX_ATTEMPTS))
    attempts = int(max_attempts if max_attempts is not None else limit)
    if attempts < 1 or attempts > 100:
        raise JobQueueError("max_attempts must be between 1 and 100")

    job = Job(
        kind=kind,
        payload=_encode_payload(payload or {}),
        status=STATUS_QUEUED,
        attempts=0,
        max_attempts=attempts,
        available_at=_now(),
    )
    db.session.add(job)
    db.session.commit()
    return job


def claim_next() -> Job | None:
    """Return the next due queued job, or ``None`` when the queue is empty."""
    if not bool(_config("JOB_QUEUE_ENABLED", True)):
        return None
    return (
        Job.query.filter(Job.status == STATUS_QUEUED, Job.available_at <= _now())
        .order_by(Job.available_at, Job.id)
        .with_for_update(skip_locked=True)
        .first()
    )


def run_job(job: Job) -> Job:
    """Run one job, recording success or scheduling a bounded retry."""
    handler = _HANDLERS.get(job.kind)
    job.attempts += 1
    job.status = STATUS_RUNNING
    job.started_at = _now()
    db.session.commit()

    try:
        if handler is None:
            raise JobQueueError(f"no handler registered for {job.kind!r}")
        handler(_decode_payload(job.payload))
    except Exception as exc:
        logger.exception("Job %s (%s) failed on attempt %s", job.id, job.kind, job.attempts)
        job.last_error = f"{type(exc).__name__}: {exc}"[:2000]
        job.finished_at = _now()
        if job.attempts >= job.max_attempts:
            job.status = STATUS_FAILED
        else:
            job.status = STATUS_QUEUED
            job.available_at = _now() + timedelta(seconds=retry_delay_seconds(job.attempts))
    else:
        job.status = STATUS_SUCCEEDED
        job.last_error = None
        job.finished_at = _now()

    db.session.commit()
    return job


def run_due(*, limit: int | None = None) -> dict:
    """Process due jobs in order and return per-outcome counts."""
    refresh_queue_metrics()
    batch = int(limit if limit is not None else _config("JOB_QUEUE_BATCH_SIZE", DEFAULT_BATCH_SIZE))
    batch = max(1, batch)
    counts = {"processed": 0, "succeeded": 0, "retried": 0, "failed": 0}
    for _ in range(batch):
        job = claim_next()
        if job is None:
            break
        run_job(job)
        counts["processed"] += 1
        if job.status == STATUS_SUCCEEDED:
            counts["succeeded"] += 1
        elif job.status == STATUS_FAILED:
            counts["failed"] += 1
        else:
            counts["retried"] += 1
    refresh_queue_metrics()
    return counts


def refresh_queue_metrics() -> None:
    """Publish current job counts per status for the queue-depth alert.

    Called by the worker and by the ``/metrics`` scrape. It is a single grouped
    count, not one query per status.
    """
    from sqlalchemy import func

    rows = db.session.query(Job.status, func.count(Job.id)).group_by(Job.status).all()
    counts = {status: int(count) for status, count in rows}
    for status in (STATUS_QUEUED, STATUS_RUNNING, STATUS_SUCCEEDED, STATUS_FAILED):
        JOBS_QUEUE_DEPTH.set(counts.get(status, 0), status=status)


def list_jobs(*, status: str | None = None, limit: int = 50) -> list[Job]:
    """Return recent jobs for operator inspection, newest first."""
    query = Job.query
    if status:
        query = query.filter(Job.status == status)
    return query.order_by(Job.id.desc()).limit(max(1, min(int(limit), 500))).all()


def _get_project(payload: dict) -> Project:
    project_id = payload.get("project_id")
    if not isinstance(project_id, int):
        raise JobQueueError("project_id must be an integer")
    project = db.session.get(Project, project_id)
    if project is None:
        raise JobQueueError(f"project {project_id} not found")
    return project


@register_handler("export.project")
def export_project(payload: dict) -> dict:
    """Build a project archive in the worker and return its byte size."""
    from app.services.exporting import iter_export_zip

    project = _get_project(payload)
    total = sum(len(chunk) for chunk in iter_export_zip(project))
    return {"project_id": project.id, "bytes": total}


@register_handler("scan.project")
def scan_project(payload: dict) -> dict:
    """Recompute a project's stored file count and total size."""
    from sqlalchemy import func

    project = _get_project(payload)
    file_count, total_size = (
        db.session.query(func.count(ProjectFile.id), func.coalesce(func.sum(ProjectFile.size), 0))
        .filter(ProjectFile.project_id == project.id)
        .one()
    )
    project.file_count = int(file_count)
    project.total_size_bytes = int(total_size)
    db.session.commit()
    return {
        "project_id": project.id,
        "file_count": project.file_count,
        "bytes": project.total_size_bytes,
    }


def validate_webhook_url(url: str) -> str:
    """Return ``url`` when it is a public HTTP(S) target, else raise."""
    if not isinstance(url, str) or not url.strip():
        raise JobQueueError("webhook url is required")
    parsed = urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise JobQueueError("webhook url must be http(s)")
    if parsed.username or parsed.password:
        raise JobQueueError("webhook url must not contain credentials")
    try:
        addresses = socket.getaddrinfo(parsed.hostname, parsed.port or 443)
    except OSError as exc:
        raise JobQueueError("webhook host cannot be resolved") from exc
    for address in addresses:
        ip = ipaddress.ip_address(address[4][0])
        if (
            ip.is_private
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_reserved
            or ip.is_multicast
            or ip.is_unspecified
        ):
            raise JobQueueError("webhook host must be publicly routable")
    return url.strip()


@register_handler("webhook.deliver")
def deliver_webhook(payload: dict) -> dict:
    """POST ``payload`` to a validated webhook URL without following redirects.

    When ``payload["secret"]`` is present the exact request body is signed with
    HMAC-SHA256 and a timestamp, and the signature headers are attached so the
    receiver can verify authenticity and reject replays.
    """
    try:
        url = validate_webhook_url(payload.get("url"))
        body = payload.get("payload")
        if not isinstance(body, dict):
            raise JobQueueError("webhook payload must be a JSON object")
        timeout = float(_config("JOB_QUEUE_WEBHOOK_TIMEOUT", 10))
        secret = payload.get("secret")
        subscription_id = payload.get("subscription_id")
        if secret is None and subscription_id is not None:
            try:
                subscription = db.session.get(WebhookSubscription, int(subscription_id))
            except (TypeError, ValueError) as exc:
                raise JobQueueError("webhook subscription_id must be an integer") from exc
            if subscription is None or not subscription.is_active:
                raise JobQueueError("webhook subscription is missing or inactive")
            event_type = payload.get("event")
            if isinstance(event_type, str) and not subscription.matches(event_type):
                raise JobQueueError("webhook subscription does not match the event")
            secret = subscription.signing_secret()
        if secret is None:
            response = requests.post(
                url,
                json=body,
                timeout=timeout,
                allow_redirects=False,
            )
        else:
            raw = json.dumps(body, separators=(",", ":"), sort_keys=True).encode("utf-8")
            headers = {
                "Content-Type": "application/json",
                **webhook_signing.signature_headers(secret, raw),
            }
            response = requests.post(
                url,
                data=raw,
                headers=headers,
                timeout=timeout,
                allow_redirects=False,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"webhook returned HTTP {response.status_code}")
    except Exception:
        WEBHOOK_DELIVERIES.inc(result="failure")
        raise
    WEBHOOK_DELIVERIES.inc(result="success")
    return {"status_code": response.status_code}


__all__ = [
    "JobQueueError",
    "claim_next",
    "deliver_webhook",
    "enqueue",
    "export_project",
    "get_handler",
    "list_jobs",
    "refresh_queue_metrics",
    "register_handler",
    "registered_kinds",
    "retry_delay_seconds",
    "run_due",
    "run_job",
    "scan_project",
    "validate_webhook_url",
]
