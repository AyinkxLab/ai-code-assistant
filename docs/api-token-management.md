# API token rotation and revocation

Personal API tokens are managed at `/settings/tokens` (linked in the header as
**API tokens**). Scopes and bearer authentication are documented in
[Scoped API tokens](api-tokens.md).

## What the page shows

The list contains metadata only and never the secret:

| Field | Meaning |
| ----- | ------- |
| `label` | User-chosen name, shown first. |
| `prefix` | First 12 characters of the secret, so tokens can be told apart. |
| `scopes` | Granted scopes (`read`, `write`, `admin`) and optional `domain:action` capability scopes. |
| `created_at` | When the token was created. |
| `last_used_at` | Updated on every authenticated request; `null` until first use. |
| `expires_at` | Optional expiry; expired tokens are inactive. |
| `revoked_at` | When the token was revoked (kept for audit history). |
| `is_active` | `false` for revoked or expired tokens. |

The secret itself is returned by exactly two responses: create and rotate. It
is never part of a list, a detail response, or an audit record.

## Operations

All endpoints are under `/settings/tokens/api` and require a session; they are
not token-authenticated.

| Method | Path | Effect |
| ------ | ---- | ------ |
| `GET` | `/settings/tokens/api` | List the caller's token metadata. |
| `POST` | `/settings/tokens/api` | Create a token (`label`, `scopes`) and return `{token, secret}` once. |
| `POST` | `/settings/tokens/api/<id>/rotate` | Replace the secret and return `{token, secret}` once. |
| `DELETE` | `/settings/tokens/api/<id>` | Revoke the token (the UI asks for confirmation first). |

Errors use the [catalog](api-errors.md): invalid scopes or missing scopes are
`400 invalid_input` (including a capability the owner does not hold), another
user's token is `404 not_found`, and rotating or revoking an already revoked
token is `409 conflict`.

The creation form offers the three legacy ranks and the owner's capability
values. Selecting a capability makes the token least-privilege; the API prefix
and HTTP method determine the single capability required for each request, as
documented in [Scoped API tokens](api-tokens.md).

## Rotation

Rotation keeps the same row (and therefore the same audit history and
timestamps) and replaces only the stored hash and display prefix. The old
secret stops working immediately; the new one is shown once.

## Revocation and audit

Revocation sets `revoked_at` and keeps the row — tokens are never deleted, so
the audit trail stays intact. The UI requires an explicit confirmation before
sending the request.

Each lifecycle action appends an audit entry through
`app/services/audit.py`:

| Action | When |
| ------ | ---- |
| `api_token_created` | A token is created. |
| `api_token_rotated` | A token's secret is rotated. |
| `api_token_revoked` | A token is revoked. |

Entries record the token id, label, and scopes only. The secret and its hash are
never written to the audit log.
