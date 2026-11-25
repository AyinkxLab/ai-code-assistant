# Organization admin console

Phase 11 adds a small organization console on top of the existing workspace
model. The repository has no separate `organizations` table: a workspace is the
organization, `Workspace.user_id` is the organization owner, and an active
`WorkspaceMember` with the `owner` or `admin` role is an organization admin.

Regular workspace members (`viewer`, `contributor`) and outsiders cannot use the
console. Every refusal is a structured error from
[the error catalog](api-errors.md): `forbidden` (403), `not_found` (404),
`invalid_input` (400), or `conflict` (409).

## Pages

| Route | Purpose |
| --- | --- |
| `GET /admin/organizations/<workspace_id>` | Single overview: member count, workspace count, estimated usage, and open high/critical security findings. |
| `GET /admin/organizations/<workspace_id>/members` | Member list, invite form, pending invitations, and member actions. |
| `GET /admin/organizations/<workspace_id>/usage` | Organization usage and budget rollups for the selected window. |

The workspace detail page shows an **Organization admin** link to owners and
organization admins.

## Member management (#627)

Owners and admins can:

- **Invite** an email address as `viewer`, `contributor`, or `admin`. The
  invitation reuses the existing hashed-token flow; the raw token is only sent
  to the invitee by email and never stored in plaintext.
- **Suspend** an active member. Suspension is the model's soft removal: the
  membership row is kept, access stops immediately, and the member can be
  restored.
- **Remove** a member. Removal is also a soft removal, but additionally cancels
  that member's pending invitations.
- **Change a role** between the assignable roles.
- **Restore** a suspended or removed member.

The owner row cannot be suspended, removed, or have its role changed. Every
mutating action requires the `confirm` checkbox in the UI; without it the server
returns `invalid_input`. The forms set `aria-busy` and disable the submit button
while the request is in flight so slow requests have a visible loading state.
Empty member and invitation lists render an explicit empty state.

## Usage and billing (#633)

The usage page reuses the Phase 10 ledger queries (`usage_ledger` and
`budgets`). It summarizes `UsageRecord` rows attributed to the workspaces owned
by the organization owner, and evaluates active monthly workspace budgets.

Cost and usage are always **estimates**: cost comes from the configured price
table, not a provider invoice. The page labels the estimate, shows how many
rows contain estimated token counts, and marks budgets that reached their
warning threshold or limit.

## Authorization

Authorization is centralized in `app/services/organizations.py`:

- `organization_role(workspace_id, user)` resolves `owner`, `admin`, or `None`.
- Owners resolve from `Workspace.user_id`; admins resolve from an active
  membership with role `owner` or `admin`.
- Unknown workspaces and non-admin callers fail closed.

The `admin` membership role is an organization role only. It does not widen the
workspace capability matrix in `app/services/permissions.py`, so adding an
organization admin never grants workspace content capabilities implicitly.

Every invite acceptance, role change, restore, suspension, and removal writes a
`role.changed` event with the before/after roles, acting admin, target member,
and request id. Owners can review those events in the workspace audit view; see
the [audit and compliance guide](audit-compliance-guide.md#role-change-events).

## Tests

`tests/test_org_admin.py` covers owner/admin access, non-admin refusals, invalid
input, the structured error codes, the dashboard cards, usage rollups, and the
invite/suspend/remove/role-change actions.
