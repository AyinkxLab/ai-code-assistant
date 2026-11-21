# AI Code Assistant Python SDK

Dependency-free Python client for the AI Code Assistant public API.

## Install

```bash
pip install -e sdk/python
```

## Quick start

```python
from ai_code_assistant import Client

client = Client(
    "https://api.example.com",
    api_token="token",
    timeout=15,
    max_retries=2,
)

project = client.get_project(42)
print(project.name)

for review in client.list_reviews(project_id=project.id):
    print(review.status, review.quality_score)
```

## Behavior

- The token is sent as `Authorization: Bearer <token>`.
- `timeout` is per attempt and applies to the whole request.
- `max_retries` is bounded. Retries only happen for idempotent requests (`GET`,
  `HEAD`, `OPTIONS`, `PUT`, `DELETE`) and for transient status codes
  (`429`, `500`, `502`, `503`, `504`) or transport failures. Non-idempotent
  requests (`POST`, `PATCH`) are never retried automatically.
- `Retry-After` is honored (capped by `max_backoff`) and exponential backoff is
  used otherwise.
- Errors are typed (`ValidationError`, `AuthenticationError`,
  `PermissionDeniedError`, `NotFoundError`, `ConflictError`, `RateLimitError`,
  `ServerError`, `TransportError`, `APITimeoutError`) and keep the raw `code`,
  `status_code` and `request_id`.

The error mapping is shared with the TypeScript SDK through
[`sdk/fixtures/error-mapping.json`](../fixtures/error-mapping.json).

## Webhook verification

```python
from ai_code_assistant import verify_signature

signature = request.headers["X-AICA-Signature"]
timestamp = int(request.headers["X-AICA-Timestamp"])
if not verify_signature(secret, request.get_data(), signature, timestamp):
    raise ValueError("invalid webhook signature")
```

The signature covers `<timestamp>.<raw body>`; verify the raw bytes before
parsing. Timestamps outside the 300-second tolerance are rejected.

## Tests

The SDK is covered by `tests/test_sdk_python.py` in the repository root, which
runs against a local mock HTTP server and requires no network access:

```bash
pytest tests/test_sdk_python.py
```
