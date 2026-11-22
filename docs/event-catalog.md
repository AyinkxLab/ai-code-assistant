# Webhook event catalog

`app/services/event_catalog.py` is the source of truth for webhook event names,
required payload fields, and retry safety. `CATALOG_VERSION` changes when a
required field changes in a way consumers must notice; adding an optional field
does not require a bump.

| Event | Area | Retry-safe | Required fields |
| ----- | ---- | ---------- | --------------- |
| `project.created` | project | yes | `project_id`, `workspace_id` |
| `project.analysis.completed` | project | yes | `project_id`, `status` |
| `review.completed` | review | yes | `review_id`, `project_id`, `status` |
| `review.failed` | review | yes | `review_id`, `project_id`, `error` |
| `usage.recorded` | usage | no | `workspace_id`, `total_tokens` |
| `member.invited` | member | yes | `workspace_id`, `invitation_id` |
| `member.removed` | member | yes | `workspace_id`, `user_id` |
| `security.finding` | security | yes | `project_id`, `severity`, `title` |
| `security.finding.resolved` | security | yes | `finding_id`, `project_id` |

Use `validate_payload(name, payload)` before delivering, and `is_retry_safe(name)`
to decide whether a transient failure may be retried. Events that are not
retry-safe must be deduplicated by their business key instead.