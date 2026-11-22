# Webhooks

Webhook subscriptions let a workspace receive signed notifications for analysis,
review, and deployment events. This page documents the subscription model and
the delivery signature contract.

## Subscription model

`WebhookSubscription` (`app/models/webhook.py`) stores:

| Field | Meaning |
| ----- | ------- |
| `workspace_id` | Workspace the subscription belongs to. |
| `url` | Absolute HTTPS endpoint; embedded credentials are rejected. |
| `event_type` | Legacy single event name (`*` matches every event). |
| `secret` | Signing secret used by the delivery worker. |
| `secret_hash` | SHA-256 of the signing secret for rotation/audit checks. |
| `is_active` | Whether deliveries are currently sent. |

Event filters live in `webhook_subscription_events` (one row per event type) and
are indexed by event type so a worker can find active subscriptions for one
event without parsing a string. When no explicit filters exist, the legacy
`event_type` column is used. A weak-network target is additionally checked at
delivery time by `app.services.job_queue.validate_webhook_url`, which rejects
private, loopback, link-local, and otherwise non-public addresses.

The extension migration is
`migrations/versions/f3a4b5c6d7e8_add_webhook_subscriptions.py`; it also merges
the two pre-existing migration heads.

## Signing

Each delivery is signed over the exact bytes sent:

    <timestamp>.<raw body bytes>

- Algorithm: HMAC-SHA256 with the per-subscription secret.
- Signature header: `X-AICA-Signature: v1=<lowercase hex>`.
- Timestamp header: `X-AICA-Timestamp: <unix seconds>`.
- Replay protection: reject timestamps outside the tolerance window (300
  seconds by default).

`app/services/webhook_signing.py` implements signing and constant-time
verification on the server. The Python, TypeScript, and Rust SDKs expose matching
helpers; see [SDKs](sdk.md#webhook-signatures).

> Verify the raw request body before parsing or re-encoding JSON. Any
> transformation changes the bytes and invalidates the signature.


## Subscription API (#668)

All routes are owner-scoped: a non-owner receives `404`.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/webhooks/api/workspaces/<workspace_id>/subscriptions` | List subscriptions |
| `POST` | `/webhooks/api/workspaces/<workspace_id>/subscriptions` | Create |
| `PATCH` | `/webhooks/api/workspaces/<workspace_id>/subscriptions/<id>` | Update |
| `DELETE` | `/webhooks/api/workspaces/<workspace_id>/subscriptions/<id>` | Delete |

Create body:

```json
{ "url": "https://example.com/hooks", "event_type": "review.completed" }
```

Validation:

- `url` must pass the job queue's SSRF guard (public HTTP(S), no credentials).
- `event_type` defaults to `*` and is limited to 100 characters.
- A signing `secret` is generated when omitted and returned **only once**, on
  creation. List, update, and delete responses never include it.

Errors are structured: `{ "error": "...", "code": "invalid_webhook_config" }`.

## Delivery logs (#672)

`GET /webhooks/api/subscriptions/<id>/deliveries` lists attempts with status,
response code, latency, and a short error label. Pagination and `status`/date
filters are supported. Response bodies and secrets are never stored or returned.

## Dead-letter queue and replay (#671)

Permanently failed deliveries can be recorded in `webhook_dead_letters` with a
JSON payload, attempts, response code, error, and an expiry timestamp.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/webhooks/api/workspaces/<workspace_id>/subscriptions/<id>/dead-letters` | List retained failures |
| `POST` | `/webhooks/api/workspaces/<workspace_id>/subscriptions/<id>/dead-letters/replay` | Replay a batch |

Replay body: `{ "ids": [1, 2, 3] }`.

- At most 50 deliveries per batch.
- Duplicate replays are skipped.
- Entries past `expires_at` are skipped (default retention is 7 days).
- Each replayed entry records the actor (`replayed_by_id`) and timestamp.
- Enqueue failures are counted, not lost; the response reports
  `replayed`, `duplicates`, `expired`, `failed`, and `missing`.

Invalid batches return `{ "error": "...", "code": "invalid_replay_request" }`.


## Payload size limits (#674)

Outbound bodies are measured on the exact UTF-8 JSON bytes that are signed and
sent. The cap is `WEBHOOK_MAX_PAYLOAD_BYTES` (default 64 KiB).

Policy:

- **Reject by default.** A payload over the cap raises a structured
  `WebhookPayloadTooLarge` (`code = "webhook_payload_too_large"`, HTTP 413 in
  API callers) and is never posted.
- **Opt-in truncation.** A producer that owns the event schema may pass
  `truncate_fields` when enqueuing/delivering. Only those optional string/list
  fields are shortened, and the truncation is reported to the caller.
- **Required fields are never truncated.** Naming a field in both
  `required_fields` and `truncate_fields` is a programming error and raises
  `WebhookPayloadError`.
- If shortening every allowed field still does not fit, the payload is rejected
  rather than silently dropping data.

`app/services/webhook_payloads.py` implements this policy; the job-queue webhook
handler applies it before any network I/O. Boundary behavior is covered by
`tests/test_webhook_payloads.py` (exactly-at-limit is accepted, one byte over is
rejected).

## Receiver contract (#675)

A receiver must:

1. Read the raw request body before parsing JSON.
2. Verify `X-AICA-Signature` over `<X-AICA-Timestamp>.<raw body>` using the
   subscription secret, and reject a timestamp outside the tolerance window.
3. Return any `2xx` status to acknowledge. Any other status (or a network
   error/timeout) is a failed attempt.
4. Be idempotent: retries and owner-triggered replays may deliver the same event
   more than once. The delivery log records each attempt separately.

Failure behavior:

- Transient failures are retried with bounded, jittered exponential backoff.
- Exhausted deliveries are retained in the dead-letter queue for owner replay.
- Replay is bounded (50 per batch), skips duplicates and expired entries, and
  records the actor.
- Response bodies and secrets are never stored in delivery logs or dead letters.

## End-to-end tests

`tests/test_webhook_e2e.py` starts a loopback-only HTTP receiver (no live
third-party service) and exercises subscription creation, signed delivery,
signature verification, retry after `5xx` responses, permanent failure
retention, and dead-letter replay:

```bash
python -m pytest tests/test_webhook_e2e.py tests/test_webhook_payloads.py -o addopts=""
```
