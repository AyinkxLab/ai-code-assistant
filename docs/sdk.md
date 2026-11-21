# SDKs (Phase 12)

Typed client libraries for the AI Code Assistant public API. Both SDKs share a
single error-mapping contract so the same server failure produces the same
typed error in every language.

| SDK | Source | Tests |
| --- | ------ | ----- |
| Python | [`sdk/python`](../sdk/python/README.md) | `tests/test_sdk_python.py` |
| TypeScript | [`sdk/typescript`](../sdk/typescript/README.md) | `tests/js/sdk_typescript.test.js` |

## Shared error contract

The mapping lives in
[`sdk/fixtures/error-mapping.json`](../sdk/fixtures/error-mapping.json):

- A server `code` (for example `insufficient_scope`) takes precedence over the
  HTTP status.
- Otherwise the HTTP status selects the typed error.
- Every error keeps the raw `code`, `status_code`, the `X-Request-ID` (or the
  body `request_id`) and the decoded payload for support and debugging.

| Status | Error |
| ------ | ----- |
| 400, 422 | `ValidationError` |
| 401 | `AuthenticationError` |
| 403 | `PermissionDeniedError` |
| 404 | `NotFoundError` |
| 409 | `ConflictError` |
| 429 | `RateLimitError` |
| 500, 502, 503, 504 | `ServerError` |

Transport failures raise `TransportError`; a timeout raises `APITimeoutError`
(a `TransportError` subclass).

## Retry policy

Retries are deliberately narrow so a client can never duplicate a write:

- Only idempotent methods (`GET`, `HEAD`, `OPTIONS`, `PUT`, `DELETE`) are
  retried.
- Only transient statuses (`429`, `500`, `502`, `503`, `504`) and transport
  failures are retried.
- `max_retries` is bounded (default `2`, i.e. at most three attempts).
- `Retry-After` is honored when present and capped by the configured maximum
  backoff; otherwise exponential backoff is used.

## Authentication and timeouts

Both clients send `Authorization: Bearer <token>` when a token is configured.
The timeout is configurable per attempt (`timeout` seconds in Python,
`timeoutMs` in TypeScript).

## Quickstart examples

Runnable examples live next to each SDK and work against a bundled mock API:

- Python: [`sdk/python/examples/quickstart.py`](../sdk/python/examples/quickstart.py),
  with a local server in
  [`sdk/python/examples/mock_server.py`](../sdk/python/examples/mock_server.py).
- TypeScript: [`sdk/typescript/examples/quickstart.ts`](../sdk/typescript/examples/quickstart.ts).

They demonstrate authentication, listing projects with pagination, structured
errors, and webhook delivery logs. Start the mock server, then run an example:

```bash
python sdk/python/examples/mock_server.py
npx tsx sdk/typescript/examples/quickstart.ts
```

## Running the tests

```bash
# Python
pytest tests/test_sdk_python.py tests/test_sdk_quickstart.py

# TypeScript (from the repository root; uses the existing Vitest setup)
npm test
```

Both suites start a local mock HTTP server, so they do not touch the network.

## Releasing

The SDKs are published from an `sdk-vX.Y.Z` tag by the `Release SDK` workflow.
Credentials, the version-bump checklist, verification, and rollback are
documented in [`sdk-release.md`](sdk-release.md).
