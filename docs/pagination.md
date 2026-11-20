# Pagination contract (Phase 12, #700)

Workspace collections can be large, so list endpoints share one bounded
pagination contract implemented in
[`app/services/pagination.py`](../app/services/pagination.py).

## Request parameters

| Parameter | Meaning |
| --- | --- |
| `page` | Optional 1-based page number. Missing or less than `1` means page `1`. |
| `limit` | Optional page size. Defaults to `50`; values above `100` are clamped to `100`. Values below `1` mean `1`. |
| `cursor` | Optional opaque token returned as `next_cursor` on the previous page. When present it continues after the last row of that page. |

`limit` may not exceed `MAX_PAGE_SIZE` (`100`); the response reports the
effective limit in `pagination.limit` / `limit`, so a client can never receive
an unbounded page.

## Stable cursors

Cursors are URL-safe base64 tokens over a small, endpoint-defined key (for
example `{"id": 42}`). They are stable because the query continues from the
last seen key instead of an offset:

- rows inserted before the cursor do not shift later pages;
- the same cursor always returns the same slice while rows are append-only;
- a cursor is not a page number and must not be interpreted by clients.

When more rows exist the response includes `next_cursor`; it is `null` on the
last page.

## Responses

```json
{
  "items": [],
  "total": 1234,
  "page": 1,
  "limit": 50,
  "next_cursor": "eyJpZCI6NTB9"
}
```

The plugin list keeps its existing envelope and adds `pagination.next_cursor`:

```json
{
  "plugins": [],
  "pagination": {
    "page": 1,
    "limit": 50,
    "max_limit": 100,
    "total": 1234,
    "pages": 25,
    "has_next": true,
    "has_prev": false,
    "next_cursor": "eyJpZCI6NTB9"
  }
}
```

## Errors

A non-integer `page`/`limit`, a malformed cursor, or a cursor whose payload does
not have the endpoint's expected shape returns HTTP `400` with a structured
body instead of a stack trace:

```json
{
  "error": "Invalid cursor.",
  "code": "invalid_pagination"
}
```

Oversized `limit` values are clamped rather than rejected (see the existing
plugin/marketplace tests); malformed values are rejected.

## Endpoints using this contract

| Endpoint | Ordering | Cursor key |
| --- | --- | --- |
| `GET /plugins/api/workspaces/<id>/plugins` | plugin id ascending | `{"id": "<plugin id>"}` |
| `GET /plugins/api/workspaces/<id>/plugin-executions` | audit row id descending | `{"id": <audit row id>}` |

The workspace audit endpoint (`/workspaces/api/workspaces/<id>/audit`) keeps
its page/per-page contract for backward compatibility.

## Scale tests

[`tests/test_pagination_at_scale.py`](../tests/test_pagination_at_scale.py)
seeds 1,200 plugins and 1,000 audit rows, asserts that a page never exceeds
`100` items, walks cursors without duplicates, verifies cursor stability across
an insert, and bounds the wall-clock time of a large-workspace request.
