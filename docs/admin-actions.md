# Admin action audit view

`GET /admin/actions` is a read-only, admin-only view of the append-only
`AuditLog` table. It exists for operators who need a filtered investigation
view; there is no edit or delete route.

## Filters

| Query parameter | Meaning |
| --------------- | ------- |
| `actor` | Exact username. Unknown names intentionally return an empty page. |
| `action` | Exact action name, for example `login_success`. |
| `start` | Inclusive lower date, `YYYY-MM-DD`. |
| `end` | Inclusive upper date, `YYYY-MM-DD`. |
| `page` / `per_page` | Pagination; `per_page` is capped at 100 (default 50). |

Filters combine with AND. `format=json` returns the same whitelisted rows for
scripting.

## What is exposed

Only `created_at`, actor username, `action`, `target_type`, and `target_id` are
rendered. The audit metadata blob is deliberately excluded: it may contain
emails or other request context and must not be treated as a public field.
Secrets and full request bodies are never stored in `AuditLog.metadata`, but
this view does not rely on that; it whitelists columns and does not read the
blob at all.

## Exporting audit records (#607)

`GET /admin/audit/export` streams a filtered export for compliance review:

| Query parameter | Meaning |
| --------------- | ------- |
| `format` | `json` (default) or `csv`. |
| `actor` | Exact username. Unknown names return no rows. |
| `action` | Exact action name. |
| `resource_type` / `resource_id` | Exact target type and id. |
| `start` / `end` | Inclusive `YYYY-MM-DD` dates. |
| `limit` | Maximum rows, 1–50000 (default 10000). |

Rows are streamed in batches and stop at `limit`; the cap is reported in the
`X-Export-Limit` response header. The projection is fixed and passes through
`app.services.classification` at the `internal` level, so the actor username,
the metadata blob, secrets, and prompt content are never exported. Each export
appends an `audit_exported` audit entry for the admin who ran it.

Invalid filters return the structured `invalid_input` error; non-admins get
`forbidden`.

## Authorization

`login_required` plus the shared `admin_required` decorator. Unauthenticated
requests are redirected to login by Flask-Login; authenticated non-admins get a
structured `403 forbidden` catalog response. Reads are append-only: no verb
other than `GET` is registered for the route.
