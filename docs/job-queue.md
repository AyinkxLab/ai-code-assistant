# Background job queue

Issue #699 adds a persisted, bounded worker queue for work that should not run
inside a request: project exports, project scans, and webhook deliveries.

Implementation: `app/services/job_queue.py`, model `app/models/job.py`, CLI
`app/services/jobs_cli.py`, migration
`migrations/versions/c9e1f2a3b4d5_add_background_jobs.py`.

## Job lifecycle

| Status | Meaning |
| ------ | ------- |
| `queued` | Waiting; `available_at` is the earliest run time. |
| `running` | Claimed by a worker. |
| `succeeded` | Handler returned; terminal. |
| `failed` | `max_attempts` exhausted; terminal, `last_error` kept. |

`attempts` counts handler executions. A failed attempt below `max_attempts`
puts the job back in `queued` with `available_at = now + min(base *
2^(attempts-1), max)`; the default base is 10 seconds and the default cap is one
hour. Payloads are JSON objects and are never printed by the CLI.

## Enqueueing from code

```python
from app.services.job_queue import enqueue, register_handler

@register_handler("report.build")
def build_report(payload):
    ...

job = enqueue("report.build", {"report_id": 7}, max_attempts=3)
```

`enqueue` raises `JobQueueError` (a `ValueError`) for an unknown kind, a
non-object or non-serializable payload, an oversized payload, or an invalid
attempt count. Handlers are looked up at run time, so a worker process must
import/register the same handlers as the producer.

Built-in kinds:

| Kind | Payload | Handler |
| ---- | ------- | ------- |
| `export.project` | `{"project_id": int}` | Builds the stored project archive and returns its byte size. |
| `scan.project` | `{"project_id": int}` | Recomputes `file_count` / `total_size_bytes` from stored `ProjectFile` rows. |
| `webhook.deliver` | `{"url": str, "payload": object}` | POSTs once with a timeout; redirects are refused and only public HTTP(S) hosts are accepted. |

## Running the worker

```bash
flask jobs worker --once
flask jobs worker --max-jobs 500
flask jobs worker --poll-interval 2
flask jobs list --status failed --json
```

Run the worker as its own process (for example a second container). `--once`
drains currently due jobs and exits; without it the worker sleeps
`JOB_QUEUE_WORKER_POLL_SECONDS` when the queue is empty. `--max-jobs` bounds one
worker run.

## Configuration

| Setting | Default | Purpose |
| ------- | ------- | ------- |
| `JOB_QUEUE_ENABLED` | `1` | Gates producer/worker availability. |
| `JOB_QUEUE_MAX_ATTEMPTS` | `5` | Default attempts when `enqueue` does not pass one. |
| `JOB_QUEUE_RETRY_BASE_SECONDS` | `10` | First retry delay. |
| `JOB_QUEUE_RETRY_MAX_SECONDS` | `3600` | Backoff cap. |
| `JOB_QUEUE_BATCH_SIZE` | `50` | Jobs per `run_due` batch / worker slice. |
| `JOB_QUEUE_MAX_PAYLOAD_BYTES` | `16384` | Enqueue-time payload cap. |
| `JOB_QUEUE_WORKER_POLL_SECONDS` | `5` | Empty-queue sleep for the worker. |
| `JOB_QUEUE_WEBHOOK_TIMEOUT` | `10` | Webhook POST timeout in seconds. |

## Tests

`tests/test_job_queue.py` covers enqueue validation, success, retry with
backoff, exhaustion after `max_attempts`, payload bounds, the CLI worker, and
the migrated `jobs` table/indexes.
