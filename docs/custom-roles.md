# Custom organization roles

Phase 11 exposes create, read, update, and delete operations for organization
roles through a JSON API. Roles belong to the organization owner
(`Workspace.user_id`) and inherit the workspace-scoped authorization already
used by [the organization console](organization-admin.md).

## Routes

| Method | Route | Purpose |
| --- | --- | --- |
| `GET` | `/api/workspaces/<workspace_id>/roles` | List built-in and custom roles. |
| `POST` | `/api/workspaces/<workspace_id>/roles` | Create a custom role. |
| `GET` | `/api/workspaces/<workspace_id>/roles/<role_id>` | Read one role. |
| `PUT` | `/api/workspaces/<workspace_id>/roles/<role_id>` | Update a custom role. |
| `DELETE` | `/api/workspaces/<workspace_id>/roles/<role_id>` | Delete a custom role. |

Only owners and active members with the organization `admin` role may use these
routes. Other callers receive a structured `forbidden` error; unauthenticated
callers receive `unauthorized`. A role id from a different organization is
reported as `not_found` so ids cannot be enumerated.

## Request and response

```json
{
  "name": "Auditor",
  "description": "Read-only review access",
  "capabilities": ["view_members", "view_activity"]
}
```

`name` is required on create, must be 1–64 characters, and may contain letters,
digits, spaces, `_`, and `-`. `description` is optional and capped at 255
characters. `capabilities` is optional (default `[]`) and may contain only the
workspace capability names from `app.services.permissions.CAPABILITIES`:
`view_members`, `leave_workspace`, `manage_members`, `manage_invitations`,
`manage_settings`, `transfer_ownership`, `view_audit`, `comment`,
`view_activity`, `heartbeat`, `manage_plugins`, `manage_billing`, and
`manage_integrations`. Unknown names return `invalid_input` with the offending
capability in `details`. Names are deduplicated and sorted.

A successful create returns `201` with the role object. Updates use `PUT` with
any subset of the three fields and return the full updated object. Deletes
return `{"deleted": true, "id": <role_id>}`.

## Built-in roles are immutable

The four built-in roles (`viewer`, `contributor`, `admin`, `owner`) backfilled by
[the role migration script](role-migration.md) are readable but cannot be
renamed, edited, or deleted. Mutating one returns `409 conflict` with
`details.reason = "builtin_role_protected"`. Custom roles are additive: they do
not change `role_can("admin", ...)` or `capabilities_for_role("admin")`, and the
organization `admin` membership role still grants no workspace capability by
itself.

## Audit trail

Every create, update, and delete appends an `AuditLog` row with the acting user
and exact before/after role snapshots in `metadata`:

```json
{
  "actor_id": 7,
  "owner_id": 1,
  "before": null,
  "after": {"id": 12, "name": "Auditor", "capabilities": ["view_members"]}
}
```

Actions are `organization_role_created`, `organization_role_updated`, and
`organization_role_deleted`. Role data never contains credential or token
material.

## Tests and reference

`tests/test_organization_roles_api.py` covers the full CRUD lifecycle, audit
snapshots, authorization, invalid capabilities, duplicate names, built-in
protection, cross-organization isolation, and the published OpenAPI paths:

```console
python -m pytest tests/test_organization_roles_api.py -o addopts="" -q
python scripts/render_api_docs.py
```
