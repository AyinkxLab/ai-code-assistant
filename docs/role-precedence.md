# Custom role assignment and precedence

Phase 11 custom roles add optional, least-privilege capability sets on top of
the built-in `workspace` roles. The implementation lives in
`app/services/role_assignments.py`; the organization admin endpoint is
`POST /admin/organizations/<workspace_id>/roles`.

## Scopes

| Scope | Target | Created for |
| ----- | ------ | ----------- |
| `organization` | `Workspace.user_id` | All workspaces owned by the same user |
| `team` | A `Team` id in the workspace | Users with an active `TeamMember` row |
| `workspace` | A `Workspace` id | Users with an active `WorkspaceMember` row |

Teams are named groups inside a workspace. Creating a team or adding a member
never grants capabilities by itself; it only makes a team-scoped assignment
eligible.

## Deterministic precedence

For a user in a workspace, exactly one source controls capabilities:

1. **Workspace assignment** — most specific.
2. **Team assignment** — only from teams where the user is an active member.
3. **Organization assignment** — applies across the owner's workspaces.
4. **Built-in role** — `WorkspaceMember.role` (`viewer` / `contributor`), or
   `viewer` for a team member with no custom assignment.

There is **no union** across scopes. A lower-precedence assignment is ignored
when a higher one applies, and capabilities from the built-in role are not
added to a custom role. If several team assignments apply, the one with the
highest assignment id wins; this is deterministic and avoids ambiguity.

The workspace owner (`Workspace.user_id`) is authoritative and cannot receive a
custom role. Non-members, removed members, and removed team members fail closed.

## Custom role contents

A custom role starts with the capabilities of `viewer` or `contributor`, adds an
explicit grant list, and applies an explicit deny list. It can never inherit
`owner` capabilities. Unknown capability names are rejected at creation time
with `400 invalid_input`. Denies win over grants and inherited capabilities.

## Management API

`GET /admin/organizations/<workspace_id>/roles` returns `roles`, `assignments`,
and `teams`. `POST` accepts an `action` field:

| Action | Fields | Effect |
| ------ | ------ | ------ |
| `create_team` | `name` | Create a team in the workspace |
| `add_team_member` | `team_id`, `user_id` | Add/reactivate a team member |
| `remove_team_member` | `team_id`, `user_id` | Soft-remove a team member |
| `create_role` | `scope`, `name`, `base_role`, `granted_capabilities`, `denied_capabilities`, `team_id` | Create a custom role |
| `assign` | `role_id`, `user_id`, `scope`, `team_id` | Assign/replace a role at one scope |
| `revoke` | `user_id`, `scope`, `team_id` | Remove an assignment, restoring the built-in role |

Only organization owners and admins may use the endpoint. Assignments are one
per `(scope, target, user)`: reassigning replaces the previous role rather than
unioning capabilities. Errors use the [catalog](api-errors.md): `403 forbidden`
for non-admins, `404 not_found` for missing targets, `409 conflict` for
duplicate names or already-revoked assignments, and `400 invalid_input` for
invalid scopes, roles, or capabilities.

See [team collaboration](team-collaboration.md) for the built-in capability
matrix and [organization admin](organization-admin.md) for console usage.
