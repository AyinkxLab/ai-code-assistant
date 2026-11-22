# Per-organization IP allowlist

An organization can restrict access to its workspace to a configured set of
IPv4/IPv6 ranges. The allowlist is enforced by a request guard before the view
runs, and it fails closed.

The implementation is `app/services/ip_allowlist.py`; configuration lives in
`workspace_ip_allowlists` (`app/models/workspace_ip_allowlist.py`).

## Configuration

Only the workspace owner may read or change the allowlist (`manage_settings`
capability).

| Route | Purpose |
| --- | --- |
| `GET /workspaces/api/workspaces/<workspace_id>/ip-allowlist` | Read the current configuration. |
| `PUT /workspaces/api/workspaces/<workspace_id>/ip-allowlist` | Replace it. |

Request body:

```json
{
  "enabled": true,
  "cidrs": ["203.0.113.0/24", "2001:db8::/32"]
}
```

Rules:

- `enabled` must be a boolean.
- `cidrs` must be a list of CIDR strings (at most 100).
- A single address is accepted and stored as `/32` (IPv4) or `/128` (IPv6).
- Host bits are masked to the network address, and duplicates are removed.
- An enabled allowlist must contain at least one range.
- Invalid entries return `400 invalid_input` and change nothing.

Disabling keeps the stored ranges so they can be re-enabled later:

```json
{"enabled": false}
```

Omitting `cidrs` on `PUT` keeps the existing ranges.

Every successful change appends an `ip_allowlist_updated` audit entry to the
workspace's hash chain, including the previous and new configuration.

## Enforcement

The guard applies to any route that resolves a workspace:

- Routes with a `workspace_id` argument (workspace pages, member/settings/audit
  APIs, the organization admin console).
- Routes with a `project_id` argument; the project's `workspace_id` selects the
  configuration.

It denies with the error catalog code `forbidden` (HTTP 403) and the message
"Access from your IP address is not allowed for this organization." HTML pages
render the standard 403 page.

The guard fails closed:

- An enabled allowlist with no usable ranges denies every request.
- An unparseable client address denies.
- An address outside every configured range denies.
- The allowlist is disabled or absent -> access is allowed (upgrade-safe
  default).

The client address is `request.remote_addr`. Forwarded headers
(`X-Forwarded-For`, `Forwarded`) are deliberately not trusted, because a client
can forge them unless a trusted reverse proxy strips and rewrites them. If the
application runs behind a proxy, configure the proxy to forward the real peer
address and ensure the app sees it as `remote_addr`.

### Exemptions

These paths are never blocked and must keep working for probes and page loads:

| Path | Why |
| --- | --- |
| `/health` | Health checks must work even when an operator locks the allowlist down. |
| `/static/...` | CSS, JS, and other page assets. |
| `/metrics` | Has its own bearer/admin authentication. |
| `/favicon.ico` | Browser asset. |
| `/healthz` | Reserved health alias. |

Sign-in (`/auth/login`) is not workspace-scoped, so it is not blocked by this
guard. The workspace-scoped page the user opens after sign-in is blocked.

## Lockout warning

The guard also applies to the configuration route. If you enable an allowlist
that does not include your current address, you lose access to the workspace,
including the route that would disable the allowlist. Add your admin network
first, and keep a break-glass path (direct database access or a forward proxy
with an allowed address) before enabling enforcement.

## Limitations

- The workspace list (`GET /workspaces/`) is not workspace-scoped, so workspace
  names remain visible to signed-in users. Only workspace content and APIs are
  restricted.
- The guard is evaluated per request with a small database read; there is no
  cache invalidation hook. A configuration change takes effect on the next
  request.
- The allowlist is per workspace. A user with several workspaces may have
  different configurations for each.
- There is no UI form yet; configure through the API above.

## Tests

`tests/test_ip_allowlist.py` covers CIDR validation (malformed, empty,
oversized, canonicalization), IPv4 and IPv6 matching, allowed and blocked
requests, structured errors, disabling, project-scoped routes, the exemption
list, audit entries, owner-only access, and the fail-closed empty-list case.
