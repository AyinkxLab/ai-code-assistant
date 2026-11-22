# Access review report

`GET /admin/access-review` is a read-only, admin-only snapshot of who can access
each workspace, when they were last active, and whether the access looks stale.
It is intended for periodic access and compliance reviews.

The report covers active workspace memberships plus the workspace owner. Owners
are included explicitly because they are not always materialized as
`WorkspaceMember` rows. Removed memberships never appear.

## Report fields

| Field | Meaning |
| ----- | ------- |
| `workspace_id` | Numeric workspace id. |
| `workspace` | Workspace name. |
| `user_id` | Numeric user id. |
| `username` | Account username. |
| `email` | Account email address. |
| `role` | `owner`, `contributor`, or `viewer`. |
| `last_active_at` | ISO 8601 UTC timestamp of the best-known activity (last presence heartbeat, else last login), or `null`. |
| `stale` | `true` when there is no activity or the last activity is older than the stale window. |
| `status` | Always `active` for rows in this report. |

The stale window defaults to 90 days (`DEFAULT_STALE_DAYS`).

## JSON

`GET /admin/access-review` (or `?format=json`) returns:

```json
{
  "items": [
    {
      "workspace_id": 1,
      "workspace": "Acme",
      "user_id": 4,
      "username": "sam",
      "email": "sam@example.com",
      "role": "viewer",
      "last_active_at": "2026-10-01T09:30:00+00:00",
      "stale": false,
      "status": "active"
    }
  ],
  "total": 1,
  "truncated": false,
  "stale_days": 90
}
```

Rows are ordered by workspace id and user id so repeated reports are stable.

## CSV

`GET /admin/access-review?format=csv` returns `text/csv` with a fixed column
order matching the field table above. The response carries:

- `Content-Disposition: attachment; filename="access-review.csv"`
- `Cache-Control: no-store`
- `X-Export-Truncated: 0` or `1`

## Bounds

At most `MAX_ROWS` (5000) rows are built. When more grants exist the report is
truncated, JSON sets `"truncated": true`, and CSV sets `X-Export-Truncated: 1`.

An unknown `format` value returns a structured `400 invalid_input` response.

## Authorization

`login_required` plus the shared `admin_required` decorator. Unauthenticated
requests are redirected to login; authenticated non-admins receive a structured
`403 forbidden` catalog response.
