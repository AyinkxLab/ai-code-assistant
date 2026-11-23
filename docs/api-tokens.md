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
