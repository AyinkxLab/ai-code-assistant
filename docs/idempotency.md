# POST idempotency keys

Phase 12 (#649) makes retried `POST` requests safe. A client sends an
`Idempotency-Key` header; the first request performs the side effect and stores
the response. A retry with the same key and body receives that stored response
instead of creating a duplicate. Reusing the key with a different body is
rejected with a structured `409`.

The implementation is `app/services/idempotency.py`; rows live in the
`idempotency_keys` table (`app/models/idempotency_key.py`).

## Client contract

```http
POST /auth/api/domains/challenge
Idempotency-Key: 550e8400-e29b-41d4-a716-446655440000
Content-Type: application/json

{"workspace_id": 12, "domain": "example.com"}
```

- A key is 1-255 characters from `A-Z`, `a-z`, `0-9`, and `._~:/+=-`.
- Invalid or missing keys on endpoints that require one return `400`
  `invalid_input`.
- Keys are scoped to the caller, the endpoint rule, and the key value. Two
  users, or two different endpoints, may use the same value safely.
- JSON bodies are canonicalized (sorted keys, compact separators) before
  hashing; semantically identical JSON is treated as the same request.
- A replayed response carries `Idempotency-Replayed: true`.

| Situation | HTTP | Code |
| --- | --- | --- |
| Missing/malformed key on a required endpoint | 400 | `invalid_input` |
| Same key, different request body | 409 | `conflict` |
| Same key while the first request is still running | 409 | `conflict` |
| Same key, same body, first response stored | original status | original body |

## Storage and TTL

Each row stores the key, a SHA-256 digest of the canonical request body, the
response status, a text copy of the response body, and a small allowlist of
replayable response headers. The **request body itself is never stored** and no
secret or credential is copied into the row.

Rows expire after `IDEMPOTENCY_TTL_SECONDS` (default 24 hours). Expired rows are
deleted on every access, and the same cleanup is available to operators:

```bash
flask purge-idempotency
```

The `expires_at` column is indexed. Rows are also created with a unique
constraint on `(owner, endpoint, key)`, so two concurrent requests cannot both
execute: the loser of the insert race sees the reservation and returns `409`
until the first response is stored or the row expires.

## Concurrency and failures

- The key is reserved **before** the view runs. A concurrent retry receives a
  structured `409` rather than running the side effect twice.
- Error responses (including `4xx` validation/permission failures) are stored
  like successful ones, so a retry sees the same structured error.
- If the view rolls back its transaction and removes the reservation,
  `finish()` recreates the row before storing the final response.
- Streamed and binary responses are replayed with their status but the body is
  not captured where it cannot be safely stored as text.

## Integration

The application factory installs `register_idempotency(app)`, which adds the
`before_request`/`after_request` hooks for all `POST` requests that carry the
header. Routes that must *require* a key can use:

```python
from app.services.idempotency import idempotent_required

@bp.post("/things")
@idempotent_required
def create_thing():
    ...
```

`idempotent` is available when a view needs to manage the lifecycle itself
(for example in a bare Flask app used by unit tests). Both decorators set a
marker so the global hooks do not double-process the request.

## Configuration

| Setting | Default | Purpose |
| ------- | ------- | ------- |
| `IDEMPOTENCY_TTL_SECONDS` | `86400` | How long a key and its response are retained. Invalid or non-positive values fall back to the default. |

## Tests

`tests/test_idempotency.py` covers key validation, canonical hashing, replay,
key reuse with a different body, in-progress conflicts, expiry cleanup, and the
purge command path.
