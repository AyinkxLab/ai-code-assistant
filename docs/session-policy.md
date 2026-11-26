# Organization session policy

Organization owners can opt in to stricter rules for tracked browser sessions.
The organization is the workspace owned by the account; policies are configured
per workspace and apply to its owner and active members. Without a policy row
the existing session behaviour is unchanged.

## Configure a policy

```http
GET    /api/session-policy/<workspace_id>
PUT    /api/session-policy/<workspace_id>
DELETE /api/session-policy/<workspace_id>
```

Only the workspace owner or an active `admin` member may read or change the
policy. A global `is_admin` flag is not sufficient. The `PUT` body is:

```json
{
  "max_session_age_minutes": 480,
  "idle_timeout_minutes": 30,
  "require_reauth_for_sensitive_actions": true,
  "reauth_window_minutes": 15
}
```

- `max_session_age_minutes`: maximum lifetime of a browser session, `1..10080`.
- `idle_timeout_minutes`: maximum time since the last request, `1..1440`.
- `require_reauth_for_sensitive_actions`: require a recent password check
  before selected mutating JSON requests.
- `reauth_window_minutes`: how long a password check stays fresh, `1..1440`
  (default `15`).

Omit a limit (or send `null`) to leave that limit disabled. `DELETE` removes the
policy entirely. Invalid values return the structured `invalid_input` error
(400); a non-admin receives `forbidden` (403); an unknown workspace receives
`not_found` (404).

## Enforcement

The session guard checks every authenticated request:

- A session older than `max_session_age_minutes`, or idle longer than
  `idle_timeout_minutes`, is revoked server-side and the request receives a
  structured `unauthorized` error (401). The user signs in again to continue.
- Every successful request refreshes `last_seen_at` on the server-side session.
- When `require_reauth_for_sensitive_actions` is enabled, mutating requests to
  `/auth/api/data-deletion`, `/api/approvals`, `/api/group-roles`,
  `/scim/v2/Groups`, and workspace member role changes require a fresh password
  check. Without one the request receives `unauthorized` (401).

The following JSON surfaces are protected by the re-auth rule; read-only
requests are not:

```http
POST /auth/api/data-deletion
POST /api/approvals
POST /api/group-roles/<workspace_id>
PATCH /scim/v2/Groups/<group_id>
PUT /workspaces/api/workspaces/<workspace_id>/members/<member_id>
```

## Re-authenticate

```http
POST /api/session-policy/<workspace_id>/reauth
Content-Type: application/json

{"password": "current password"}
```

Any active organization member may re-authenticate. The password is checked
against the existing hash; it is never stored or logged. Success returns the
UTC `reauthenticated_at` timestamp and refreshes the current browser session
only. An incorrect password returns `unauthorized` (401); a missing body field
returns `invalid_input` (400).

## Scope

- Only tracked browser sessions created by the sign-in flow are aged out.
  Sessions created outside that flow have no server-side row and are not
  affected.
- API tokens do not carry a browser session and are governed by their existing
  scopes, not by this policy. Rotating `SECRET_KEY` still invalidates every
  browser cookie.
- Policy changes and session expirations append to the audit chain as
  `session_policy_updated`, `session_policy_deleted`, `session_policy_expired`,
  and `session_reauthenticated`. Audit metadata records limits and reasons,
  never passwords or session tokens.
