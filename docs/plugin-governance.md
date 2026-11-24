# Organization plugin governance

Organizations can control which plugins may be installed in their workspaces.
The policy is one setting per organization (workspaces owned by the same
account) and is enforced at install time by every workspace install route:
local manifest, catalog, already-registered plugin, and marketplace artifact.

## Policy modes

| Mode | Effect |
| ---- | ------ |
| `allow` | Installs proceed normally. This is the default when no policy row exists. |
| `block` | Every install is refused with `403 forbidden` and audited. |
| `require_approval` | Installs are refused until an organization admin approves that plugin id. |

## Managing the policy

Organization admins only (the workspace owner or an active `owner`/`admin`
member):

```bash
GET /plugins/api/workspaces/<workspace_id>/plugins/policy

PUT /plugins/api/workspaces/<workspace_id>/plugins/policy
Content-Type: application/json

{"mode": "require_approval"}
```

Approving one plugin under a `require_approval` policy:

```bash
POST /plugins/api/workspaces/<workspace_id>/plugins/<plugin_id>/approve
Content-Type: application/json

{"reason": "reviewed in the security review"}
```

Approvals are per organization and plugin id. Revoking an approval is done by
switching the mode or removing the row with a follow-up migration; the install
check fails closed either way.

## Audit and errors

Policy changes, approvals, and refused installs append audit rows:

| Action | When |
| ------ | ---- |
| `plugin_policy_updated` | An admin changes the mode. |
| `plugin_install_approved` | An admin approves a plugin id. |
| `plugin_install_blocked` | An install is refused by `block` or by a missing approval. |

A non-admin receives `403 forbidden`. An unknown mode, a missing `mode`, a
non-object body, or an invalid approval reason returns `400 invalid_input`. A
non-member receives `404 not_found` (no organization existence oracle).

## Scope

The policy applies to the workspace install API. The operator CLI
(`flask plugins ...`) runs with deployment credentials and is intentionally not
subject to organization policy; restrict operator access separately.
