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

## Custom roles (#597)

Owners and organization admins can define reusable roles from the plain
capability actions in `app.services.permissions.CAPABILITIES` (for example
`view_members` or `manage_plugins`). The migration backfills three immutable
built-ins for every organization owner — `owner`, `admin`, and `member` — and
the service seeds the same rows on databases created with `db.create_all()`.
Built-in roles cannot be renamed or deleted.

| Action | Route |
| --- | --- |
| List roles and available capabilities | `GET /admin/organizations/<workspace_id>/roles` |
| Create a custom role | `POST /admin/organizations/<workspace_id>/roles` |
| Rename/change capabilities | `PATCH /admin/organizations/<workspace_id>/roles/<role_id>` |
| Delete a custom role | `DELETE /admin/organizations/<workspace_id>/roles/<role_id>` |

POST/PATCH accept JSON or form fields: `name`, a `capabilities` list (or
comma-separated string), and an optional `description`. Names are lowercased and
must be unique per owner. Unknown capabilities, invalid names, and reserved
built-in names return `invalid_input` (400) or `conflict` (409); non-admin
callers receive `forbidden` (403) and unknown roles `not_found` (404). Custom
role mutations are recorded in the audit log and adoption telemetry.

Defining a custom role does not automatically assign it to members: it remains
a named capability set until a future issue wires assignments into membership.
It also never widens the workspace capability matrix for the `admin`
membership role.

## SCIM bearer tokens (#594)

Organization owners and admins can issue, rotate, and revoke SCIM bearer tokens
from the console. Each token is scoped to the organization owner and names the
workspace used as the default provisioning target. Only a SHA-256 hash is
stored; the full secret appears once in the issue/rotate response.

| Action | Route |
| --- | --- |
| List token metadata | `GET /admin/organizations/<workspace_id>/scim-tokens` |
| Issue | `POST /admin/organizations/<workspace_id>/scim-tokens` |
| Rotate | `PATCH /admin/organizations/<workspace_id>/scim-tokens/<token_id>` |
| Revoke | `DELETE /admin/organizations/<workspace_id>/scim-tokens/<token_id>` |

POST accepts `label` and an ISO-8601 `expires_at`. Invalid input is
`invalid_input` (400), another owner's token is `not_found` (404), and rotating
a revoked token is `conflict` (409). See
[the SSO/SCIM setup guide](sso-scim-setup.md) for provisioning details.

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

## Tests

`tests/test_org_admin.py` covers owner/admin access, non-admin refusals, invalid
input, the structured error codes, the dashboard cards, usage rollups, and the
invite/suspend/remove/role-change actions.
