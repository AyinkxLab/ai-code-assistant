# Public API v1

Stable, versioned JSON resources for projects, reviews, and usage. The
implementation is `app/api/v1.py`; errors use the
[API error catalog](api-errors.md), lists use the
[pagination contract](pagination.md), and filters/sort use the
[filtering and sorting standard](api-filtering.md).

All endpoints require an authenticated session or a scoped
[API token](api-tokens.md). Unauthenticated calls return
`401 {"code": "unauthorized"}`, and a token without the required scope returns
`403 {"code": "forbidden"}`.

## Projects

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/projects` | List the caller's projects (newest id first). |
| `GET` | `/api/v1/projects/<id>` | Return one project owned by the caller. |

Query parameters: `workspace_id` (optional filter), `source`, `status`, plus
`sort`/`order` (`id` default, then `name`, `created_at`, `updated_at`) and
`page`, `limit`, and `cursor`. A project owned by another user is
`404 not_found`, matching the web UI's owner-scoped project lookup. See the
[filtering standard](api-filtering.md) for validation and error shapes.

Response item schema:

```json
{
  "id": 12,
  "workspace_id": 3,
  "name": "my-project",
  "source": "archive",
  "source_url": null,
  "status": "ready",
  "error_message": null,
  "progress": 100,
  "file_count": 42,
  "total_size_bytes": 123456,
  "indexed_at": "2026-01-01T00:00:00+00:00",
  "created_at": "2026-01-01T00:00:00+00:00",
  "updated_at": "2026-01-01T00:00:00+00:00"
}
```

## Reviews

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/v1/reviews` | List the caller's reviews (newest id first). |
| `GET` | `/api/v1/reviews/<id>` | Return one review owned by the caller. |

Query parameters: `source`, `kind`, `status`, `project_id`, plus
`sort`/`order` (`id` default, then `created_at`, `updated_at`, `kind`,
`status`) and pagination. Only the caller's reviews are visible; another
user's review is `404`.

Response item schema:

```json
{
  "id": 7,
  "user_id": 1,
  "project_id": 12,
  "source": "project",
  "kind": "quality",
  "status": "completed",
  "error_message": null,
  "owner": null,
  "repo": null,
  "pr_number": null,
  "pr_title": null,
  "base_ref": null,
  "head_ref": null,
  "summary": {"overview": "..."},
  "findings_count": 3,
  "created_at": "2026-01-01T00:00:00+00:00",
  "updated_at": "2026-01-01T00:00:00+00:00"
}
```

## Usage

`GET /api/v1/usage` returns a bounded page of the caller's usage-ledger rows.
It reuses the existing usage export service, so the schema is the same
`schema_version` document documented in
[usage privacy](usage-privacy.md): `filters`, `pagination`, `totals`,
`page_totals`, and `records`. Optional filters are `workspace_id`,
`project_id`, and `source`. Cursor pagination is not supported here; use
`page` and `limit` (a cursor returns `400 invalid_input` rather than being
silently ignored). It does not accept `sort`/`order`; those are rejected as
unknown parameters (see the [filtering standard](api-filtering.md)).

## List envelope

```json
{
  "items": [],
  "total": 42,
  "page": 1,
  "limit": 50,
  "sort": "id",
  "order": "desc",
  "next_cursor": null
}
```

`limit` defaults to 50 and is clamped to 100. A malformed `page`, `limit`,
cursor, or integer filter returns `400 invalid_input`. Treat `next_cursor` as
opaque; pass it back unchanged to continue after the last row.
