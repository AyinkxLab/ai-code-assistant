# Enterprise admin guide

This guide is the operator walkthrough for the organization features that ship
in the repository. Every route and command below is checked against the real
URL map and Click tree by `tests/test_enterprise_admin_guide.py`.

Related references:

- [Organization admin console](organization-admin.md)
- [Team collaboration](team-collaboration.md)
- [Domain verification and SSO enforcement](sso-domain-verification.md)
- [Audit and compliance guide](audit-compliance-guide.md)
- [Audit retention and legal hold](audit-retention.md)
- [Per-organization IP allowlist](ip-allowlist.md)
- [User data export](user-data-export.md)
- [User data deletion and anonymization](user-data-deletion.md)

## Organization model

There is no separate `organizations` table. A **workspace is the
organization**:

- `Workspace.user_id` is the organization owner.
- An active `WorkspaceMember` with role `owner` or `admin` is an organization
  admin in the admin console. The `admin` membership role does not widen the
  workspace capability matrix in `app/services/permissions.py`.
- Regular members (`contributor`, `viewer`) can collaborate but cannot enter the
  organization console.

Roles and capabilities:

| Role | Workspace capabilities |
| --- | --- |
| `owner` | Everything: members, invitations, settings, ownership transfer, audit, plugins, billing, integrations. |
| `contributor` | Read members and activity, comment, heartbeat, leave the workspace. |
| `viewer` | Read members and activity, comment, heartbeat, leave the workspace. |
| `admin` (organization console only) | Owner/admin organization dashboard, member management, and usage. |

## Set up an organization

1. Sign in and create a workspace at `/workspaces/` (the **New workspace**
   control) or through the API:

   ```http
   POST /workspaces/api/workspaces
   {"name": "Acme platform"}
   ```

   The creator becomes the owner.
2. Open the workspace at `/workspaces/<workspace_id>`. The **Organization
   admin** link appears for owners and organization admins.
3. The console entry point is `GET /admin/organizations/<workspace_id>`. It shows
   member count, workspace count, estimated usage for the selected window
   (`?days=`), and open high/critical security findings.
4. Apply database migrations with `flask db upgrade` before using a new release.

## Members and invitations

The HTML flow is `/workspaces/<workspace_id>/members`. The organization console
uses `GET /admin/organizations/<workspace_id>/members`; both render the same
membership data.

| Action | Route |
| --- | --- |
| List members (owner + active members) | `GET /workspaces/api/workspaces/<workspace_id>/members` |
| Add an existing user | `POST /workspaces/api/workspaces/<workspace_id>/members` |
| Change a member role | `PATCH /workspaces/api/workspaces/<workspace_id>/members/<user_id>` |
| Remove a member (soft removal) | `DELETE /workspaces/api/workspaces/<workspace_id>/members/<user_id>` |
| Leave a workspace | `DELETE /workspaces/api/workspaces/<workspace_id>/membership` |
| Transfer ownership | `POST /workspaces/api/workspaces/<workspace_id>/transfer` |
| Invite by email | `POST /workspaces/api/workspaces/<workspace_id>/invitations` |
| List/cancel invitations | `GET` / `DELETE /workspaces/api/workspaces/<workspace_id>/invitations/<invite_id>` |
| Public invitation landing | `GET /workspaces/invitations/<token>` |

Walkthrough (owner):

1. Open `/workspaces/<workspace_id>/members`.
2. Add a user by username as `viewer` or `contributor`, or invite an email
   address. Invitation emails contain a hashed token; the plaintext is never
   stored.
3. Change roles with the role selector. The owner row cannot be changed,
   removed, or suspended.
4. Removal is a soft removal: the row and history are retained, access stops
   immediately, and the member can be re-added later.
5. To leave, a non-owner uses **Leave workspace**. The owner must transfer
   ownership instead.

Organization console member actions (`POST
/admin/organizations/<workspace_id>/members`) support invite, suspend, remove,
reactivate, and role change. Mutating actions require the `confirm` field; a
missing confirmation returns `invalid_input`. Non-admin callers receive
`forbidden`; unknown workspaces receive `not_found`; conflicting state returns
`conflict`.

Workspace settings are at `GET`/`PUT
/workspaces/api/workspaces/<workspace_id>/settings` and are owner-only for
writes. They control invitations, the default member role, and MFA policy.

## Roles and access checks

Authorization is centralized:

- `app/services/permissions.py` resolves workspace roles and capabilities.
- `app/services/organizations.py` resolves organization owner/admin roles.
- Non-members always fail closed.

API denials use [the error catalog](api-errors.md): `unauthorized` (401),
`forbidden` (403), `not_found` (404), `invalid_input` (400), `conflict` (409).
HTML pages render the matching error page.

Representative owner-only surfaces:

- `GET /workspaces/api/workspaces/<workspace_id>/audit`
- `PUT /workspaces/api/workspaces/<workspace_id>/settings`
- `GET /admin/organizations/<workspace_id>`
- `GET /admin/audit`
- `GET /admin/actions`
- `GET /admin/access-review`

## Custom roles (#597)

Roles are scoped to the organization owner and named from the plain capability
actions in `app.services.permissions.CAPABILITIES`. The migration backfills the
immutable `owner`, `admin`, and `member` built-ins for every existing owner;
`ensure_builtin_roles(owner_id)` performs the same seeding for fresh
`db.create_all()` databases.

| Action | Route |
| --- | --- |
| List roles and capabilities | `GET /admin/organizations/<workspace_id>/roles` |
| Create a role | `POST /admin/organizations/<workspace_id>/roles` |
| Update a role | `PATCH /admin/organizations/<workspace_id>/roles/<role_id>` |
| Delete a role | `DELETE /admin/organizations/<workspace_id>/roles/<role_id>` |

Only owners and organization admins can use these routes. Invalid input uses
`invalid_input` (400), duplicates and changes to built-ins use `conflict`
(409), unknown roles use `not_found` (404), and other callers get `forbidden`
(403). See [the organization console](organization-admin.md) for payloads.

## SSO and domain verification

SSO enforcement proves domain ownership, then blocks password sign-in for
accounts on that domain. It does not configure an external identity provider.

1. Request a DNS challenge:

   ```http
   POST /auth/api/domains/challenge
   {"workspace_id": 12, "domain": "example.com"}
   ```

2. Publish the returned TXT record (`_aica-verification.example.com` with the
   returned `aica-verify=<token>` value).
3. Verify:

   ```http
   POST /auth/api/domains/verify
   {"workspace_id": 12, "domain": "example.com"}
   ```

4. Enforce SSO only after verification:

   ```http
   PATCH /auth/api/domains/<domain_id>
   {"workspace_id": 12, "enforce_sso": true}
   ```

5. List domains with `GET /auth/api/domains?workspace_id=12`.

Challenges expire after 24 hours. Every domain operation requires the
`manage_settings` capability (currently the owner). See
[domain verification and SSO enforcement](sso-domain-verification.md) for the
error table.

## SCIM

SCIM 2.0 **Users** provisioning is implemented at `/scim/v2/Users`, protected by
an organization bearer token issued in the admin console. Supported operations
are list/filter/paginate, get, create, patch, and deactivate; the full contract
and setup steps are in [the SSO/SCIM setup guide](sso-scim-setup.md). The SCIM
guard accepts only `Authorization: Bearer`, never `X-API-Token` or a personal
API token, and it never creates a browser login session.

`/scim/v2/Groups`, OIDC/SAML login, and automatic group-to-role sync remain
unimplemented. Member creation, role changes, suspension, and removal outside
SCIM are still performed through the member routes above.

## Audit

Every sensitive action appends to `audit_logs`. Rows are append-only through the
application and chain per workspace with SHA-256 hashes.

| View | Route |
| --- | --- |
| Workspace audit page (owner) | `GET /workspaces/<workspace_id>/audit` |
| Workspace audit JSON | `GET /workspaces/api/workspaces/<workspace_id>/audit` |
| Site-admin audit log | `GET /admin/audit` |
| Site-admin action view | `GET /admin/actions` |
| Filtered export (JSON or CSV) | `GET /admin/audit/export` |
| Access review report | `GET /admin/access-review` |

Walkthrough:

1. As the workspace owner, open `/workspaces/<workspace_id>/audit`.
2. Filter the site-admin views with `actor`, `action`, `start`, and `end`
   (`YYYY-MM-DD`).
3. Verify integrity with:

   ```bash
   flask audit verify --workspace-id 7
   flask audit verify --json
   ```

   Exit status is `0` when every chain is intact, `1` when tampering is found,
   and `2` for invalid input.

## Retention and legal hold

Audit retention is per workspace. A workspace without a policy uses 365 days and
no legal hold.

```bash
flask audit retention show --workspace-id 7
flask audit retention set --workspace-id 7 --days 90
flask audit retention set --workspace-id 7 --legal-hold
flask audit retention set --workspace-id 7 --no-legal-hold
flask audit purge --workspace-id 7 --dry-run
flask audit purge --workspace-id 7
```

`--days` accepts 1–3650. While a legal hold is active, the purge deletes
nothing. Purges remove a contiguous oldest prefix and keep the hash chain
verifiable. Schedule the purge with cron, a Kubernetes CronJob, or the platform
scheduler. See [audit retention and legal hold](audit-retention.md).

Usage-ledger retention is separate:

```bash
flask usage purge --dry-run
flask usage purge --days 365
```

The window is bounded by `USAGE_RETENTION_DAYS` (default 365).

## User data requests

Owners and members can manage their own personal data:

- Export: `GET`/`POST /auth/api/data-export` and
  `GET /auth/api/data-export/<request_id>/download`.
- Deletion or anonymization: `GET`/`POST /auth/api/data-deletion`,
  `POST /auth/api/data-deletion/<request_id>/confirm`, and
  `POST /auth/api/data-deletion/<request_id>/cancel`. Confirmation requires the
  exact phrase `delete my data`.

Deletion anonymizes the account and removes personal content; audit rows and
the account row are retained. See
[user data deletion and anonymization](user-data-deletion.md) for the exact
deleted/anonymized/retained contract.

## Operational checklist

- Run `flask db upgrade` on deploy.
- Keep `PLUGIN_TRUST_POLICY=required` and an explicit trust store in production.
- Configure `METRICS_TOKEN` and scrape `/metrics` over HTTPS.
- Run `flask audit verify` on a schedule and alert on a non-zero exit.
- Schedule `flask audit purge` only after confirming the retention window and
  legal-hold state.
- Transfer workspace ownership before an owner deletes their account.
