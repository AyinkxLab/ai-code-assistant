# Scoped API tokens

Personal API tokens authenticate the public JSON API as their owning user. The
implementation is `app/services/api_tokens.py`, the model is
`app/models/api_token.py`, and the token settings page is described in
[API token rotation and revocation](api-token-management.md).

## Authentication

Send the secret as a bearer token:

```http
Authorization: Bearer aat_…
```

`X-API-Token: aat_…` is accepted as an alternative for clients that cannot set
`Authorization`. Tokens are accepted on JSON API paths only (`/api/…` and
module APIs such as `/workspaces/api/…`); using one on an HTML page returns
`401 unauthorized` with the [catalog](api-errors.md) shape. A call with no
session and no valid token is `401`; a valid token without the required scope is
`403 forbidden`.

Only a SHA-256 hash of the secret is stored. The secret is shown once at
creation (and once more after a rotation); list and detail responses never
include it.

## Scopes

Scopes are ranked, so a higher scope includes everything below it:

| Scope | Allows |
| ----- | ------ |
| `read` | `GET`, `HEAD`, `OPTIONS` |
| `write` | everything `read` allows, plus `POST`, `PUT`, `PATCH` |
| `admin` | everything `write` allows, plus `DELETE` (destructive operations) |

At least one scope is required and unknown scope names are rejected with
`400 invalid_input`. Scope enforcement happens in authentication, before the
view runs, so a token cannot reach an operation its scopes do not grant.

### Capability scopes (#603)

Tokens can instead (or additionally) request capability scopes. A capability
scope is a `domain:action` value from the owner's capability set
(`app/services/capabilities.py`); a token can never grant a capability its owner
does not already hold. Capabilities are canonicalized after the legacy ranks.
Selecting any capability makes the token least-privilege: the legacy method
ranks are no longer used for that token and only the mapped capability for the
endpoint is accepted.

| API prefix | Required capability |
| ---------- | ------------------- |
| `/api/v1/projects`, `/workspaces/api/projects` | `project:read`, `project:write`, `project:delete` |
| `/api/v1/reviews`, `/reviews/api` | `review:read`, `review:create` |
| `/api/v1/usage` | `workspace:read` |
| `/workspaces/api/workspaces` | `workspace:read`, `workspace:write` |
| `/api/conversations` | `ai:access`, `ai:analysis` |
| `/github/api` | `github:read`, `github:write` |
| `/stellar/api` | `stellar:read`, `stellar:write` |

Safe methods (`GET`, `HEAD`, `OPTIONS`) require the domain's `read` capability
(`ai:access` for AI conversations). Mutations require `write`, except review
creation uses `review:create` and project deletion uses `project:delete`.
Unmapped API paths are denied with `403 forbidden`; the structured body includes
`details.required_capability` and `details.granted_scopes` and never contains
the token secret or hash. Creating a token with a capability outside the
owner's set returns `400 invalid_input`.

## Lifecycle

- `POST` create, rotate, and revoke are performed from
  [Settings → API tokens](api-token-management.md); the list API and page only
  return metadata (`label`, `prefix`, `scopes`, `created_at`, `last_used_at`,
  `expires_at`, `revoked_at`, `is_active`).
- Rotation replaces the stored hash in place and keeps the row, so audit history
  and timestamps survive. The old secret stops working immediately.
- Revocation is permanent for that row and keeps it for audit history.
- Expired or revoked tokens fail authentication with `401 unauthorized`.
- `last_used_at` is updated on every authenticated request.

## Session compatibility

Browser sessions keep working unchanged. A session-authenticated request that
also sends an `Authorization` header whose value is not a stored token (for
example the rate-limit quota subject used by `docs/api-rate-limits.md`) still
uses the session. Token-only requests skip the session CSRF check because they
carry no cookie.
