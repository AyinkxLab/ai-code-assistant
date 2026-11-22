# User data export requests

Users can request a copy of their own account data. The workflow is auditable,
rate limited, and the download link is one-time and expires.

## Lifecycle

`user_data_exports` rows move through these states:

| State | Meaning |
| ----- | ------- |
| `requested` | The request was recorded. |
| `processing` | The export was accepted for processing. |
| `ready` | The download link is valid until `expires_at`. |
| `downloaded` | The one-time link has been used. |
| `expired` | The download window passed. The link cannot be reused. |

The service exposes `request_export`, `process_request`, and `download_export`
in `app/services/data_export.py`. Every transition appends an audit entry
(`data_export_requested`, `data_export_ready`, `data_export_downloaded`,
`data_export_expired`).

## API

All routes require login and only ever return the current user's requests.

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `POST` | `/auth/api/data-export` | Request and process an export. |
| `GET` | `/auth/api/data-export` | List your recent requests. |
| `GET` | `/auth/api/data-export/<id>` | Read one request's status. |
| `GET` | `/auth/api/data-export/<id>/download?token=...` | Download once. |

`POST` returns `201` with `status`, the request id, a one-time
`download_token`, and the `download_path`. The plaintext token is returned once
and never stored; only its SHA-256 hash is persisted.

## Limits and safety

- At most 3 requests per user per 24 hours. Over-limit calls return `429` with a
  `Retry-After` header.
- Download links expire after 24 hours. An expired link returns `410 gone`.
- A link can be downloaded once; reuse returns `409 conflict`.
- Unknown requests, another user's requests, and bad tokens all return a uniform
  `404 not_found` so ids and links cannot be probed.
- The exported document contains the account profile, the user's conversations
  and messages, and saved prompts. Password hashes, API keys, sessions, and other
  users' records are never included.
- Responses carry `Cache-Control: no-store` and an attachment filename.
