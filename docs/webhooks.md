# Webhooks

Workspace owners can register webhook subscriptions, inspect delivery logs, and
replay permanently failed deliveries.

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
