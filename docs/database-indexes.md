# Database indexes

Phase 12 audit (issue #697) of the filters and orderings the application
actually runs. Every index below is served by a query in the codebase; indexes
that would only duplicate an existing single-column index or a unique
constraint were deliberately not added.

The single-column indexes created by the model columns stay in place. The
composite indexes below let the database satisfy the equality filter and the
`ORDER BY` (or range filter) with one index scan instead of an index lookup
plus a sort.

| Index | Table | Columns | Query it serves |
| ----- | ----- | ------- | --------------- |
| `ix_conversations_user_updated` | `conversations` | `user_id`, `updated_at` | `GET /api/conversations` and the chat sidebar: `WHERE user_id = ? ORDER BY updated_at DESC` (`app/chat/api.py`, `app/chat/routes.py`). |
| `ix_conversations_workspace_updated` | `conversations` | `workspace_id`, `updated_at` | Workspace-scoped conversation list (`?workspace_id=` on the same routes). |
| `ix_messages_conversation_created` | `messages` | `conversation_id`, `created_at` | Conversation history: `WHERE conversation_id = ? ORDER BY created_at`, including the `Conversation.messages` relationship used by the detail endpoints and exports. |
| `ix_project_messages_session_created` | `project_messages` | `session_id`, `created_at` | Project chat history: `GET /workspaces/api/projects/<id>/messages?session_id=…` filters by session and orders by `created_at` (`app/workspaces/routes.py`). |
| `ix_audit_logs_user_created` | `audit_logs` | `user_id`, `created_at` | Admin audit filter by actor plus the `created_at DESC, id DESC` page ordering (`app/admin/routes.py`). |
| `ix_audit_logs_action_created` | `audit_logs` | `action`, `created_at` | Admin audit filter by action with the same ordering. |

## Existing indexes that already cover a query

- `project_files` has a unique constraint on `(project_id, path)`, which backs
  the export's `WHERE project_id = ? ORDER BY path` instead of a second index.
- `workspace_members` has a unique constraint on `(workspace_id, user_id)`,
  which backs the per-request membership lookup in `app/services/permissions.py`.
- `notifications` and `activity_events` already have
  `(user_id, is_read, created_at)` and `(workspace_id, created_at)` composites.
- The usage ledger composites are documented in
  [Observability](observability.md#usage-ledger).

## Migration and verification

The migration is `migrations/versions/a1c2e3f4b5d6_add_phase12_read_indexes.py`
(revision `a1c2e3f4b5d6`). It only creates indexes; existing rows are untouched
and `downgrade()` drops each index in reverse order.

`tests/test_phase12_indexes.py` asserts that the models and the migrated schema
declare the same indexes, and pins the SQLite query plans for each shape above
so an accidental index removal fails the suite.
