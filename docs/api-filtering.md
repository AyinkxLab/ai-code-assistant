# Filtering and sorting standard

Public list resources accept one query grammar, implemented in
[`app/api/filtering.py`](../app/api/filtering.py). The same rules apply to every
resource: only whitelisted fields are accepted, the default ordering is
documented, and an unknown parameter is an error rather than a silently ignored
typo.

## Query parameters

| Parameter | Meaning |
| --- | --- |
| `<filter>=<value>` | Exact-match filter. The field must be on the resource whitelist. |
| `sort=<field>` | One whitelisted sort field. Defaults to the resource's default ordering (always `id`). |
| `order=asc\|desc` | Sort direction. Defaults to `desc`. |
| `page`, `limit`, `cursor` | The [pagination contract](pagination.md). |

Whitelisted fields are listed per endpoint below. A repeated parameter keeps
the last value, matching normal query-string handling.

## Ordering

The default ordering is newest id first: `sort=id&order=desc`. It is stable
because `id` is unique. A named sort always adds `id` as a tie-breaker in the
same direction, so equal values still have a deterministic order.

`cursor` continues from the id of the last row of the previous page. It is only
valid with the default ordering; combining a cursor with another sort returns
`400 invalid_input` (`Cursor pagination requires sort=id.`) instead of silently
producing gaps or duplicates. Use `page`/`limit` for custom sorts.

## Resources

### `GET /api/v1/projects`

| Filters | Sort fields |
| --- | --- |
| `workspace_id` (integer), `source`, `status` | `id`, `name`, `created_at`, `updated_at` |

Default: `sort=id&order=desc`. See the [v1 reference](api-v1.md).

### `GET /api/v1/reviews`

| Filters | Sort fields |
| --- | --- |
| `project_id` (integer), `source`, `kind`, `status` | `id`, `created_at`, `updated_at`, `kind`, `status` |

Default: `sort=id&order=desc`.

### `GET /api/v1/usage`

Usage keeps its dedicated `workspace_id`, `project_id`, and `source` filters
and its fixed ledger ordering. It does not accept `sort` or `order`; sending
either is rejected as an unknown parameter. Use the usage export filters
documented in [usage privacy](usage-privacy.md).

## Errors

An unknown parameter, an unknown sort field, a malformed value, or an invalid
order returns `400` with the shared [error catalog](api-errors.md) shape. The
`details` object names the offending field and, where useful, the accepted
values:

```json
{
  "error": "Unknown query parameter(s): stauts. Allowed: created_at, cursor, id, limit, name, order, page, sort, source, status, updated_at, workspace_id",
  "kind": "invalid",
  "code": "invalid_input",
  "details": {
    "unknown": ["stauts"],
    "allowed": ["created_at", "cursor", "id", "limit", "name", "order", "page", "sort", "source", "status", "updated_at", "workspace_id"]
  }
}
```

```json
{
  "error": "Unknown sort field: summary. Allowed: created_at, id, kind, status, updated_at",
  "kind": "invalid",
  "code": "invalid_input",
  "details": {"field": "summary", "allowed": ["created_at", "id", "kind", "status", "updated_at"]}
}
```

## Tests

[`tests/test_api_v1.py`](../tests/test_api_v1.py) covers default ordering,
whitelisted filters and sorts, unknown-parameter rejection with `details`, an
invalid `order`, the cursor/sort rule, and the unsupported usage parameters.
