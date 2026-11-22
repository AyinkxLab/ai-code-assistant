# API error catalog

All public API errors share one stable shape:

```json
{"error": "The request is invalid.", "kind": "invalid", "code": "invalid_input"}
```

`code` is the stable contract. `error` and `kind` are retained for existing
clients; do not build new logic on their wording. Internal exception text and
stack traces are never returned. Routes may supply a safe validation message
(such as a date-range hint) but the code and HTTP status stay fixed.

The catalog lives in `app/api/error_catalog.py`. Add a code there before using
it in a route; the test suite scans `app/` for `error_response(...)` calls and
fails when a code is missing.

| Code | HTTP | Kind | Default message |
| ---- | ---- | ---- | --------------- |
| `invalid_input` | 400 | `invalid` | The request is invalid. |
| `unauthorized` | 401 | `unauthorized` | Authentication required. |
| `forbidden` | 403 | `forbidden` | You do not have permission to do that. |
| `not_found` | 404 | `not_found` | The requested resource was not found. |
| `conflict` | 409 | `conflict` | The request conflicts with the current state. |
| `validation_failed` | 422 | `validation_failed` | The request could not be validated. |
| `rate_limited` | 429 | `rate_limited` | Too many requests. Please try again later. |
| `internal_error` | 500 | `internal_error` | Something went wrong. |

## Where catalog errors are produced

- The application error handlers return the catalog shape for API requests
  (paths under `/api/` and JSON-preferring clients) and the normal HTML pages
  for browser requests.
- Admin permission and invalid-input paths use `error_response(...)` directly.
- Other routes keep their existing `{"error": ...}` bodies until they are
  migrated; those responses do not yet carry a stable `code`.

## Versioning

Codes are never renamed or reused for a different meaning. A new failure mode
gets a new code. Removing a code is a breaking change and needs a deprecation
window in the release notes.
