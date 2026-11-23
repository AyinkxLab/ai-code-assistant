# API rate limits and quotas

Public API endpoints enforce per-token and per-organization quotas for each
endpoint family (the decorator's bucket, e.g. `workspaces`, `workspace-members`,
`api-v1`). Each family has its own counters, so a burst on one family does not
consume another family's organization budget.

- **Per token.** Identified by the `Authorization: Bearer <token>` or
  `X-API-Token` header. The token is hashed with SHA-256 before it is used as a
  limiter key, so the raw credential is never stored or logged. Requests without
  a token are keyed by the authenticated user id.
- **Per organization.** Identified by the `X-Organization-Id` header, the
  route's `workspace_id`, or the first positional argument.

Both checks run before the view returns data. If either is exceeded, the view
is not called.

## Settings

| Variable | Default | Meaning |
| -------- | ------- | ------- |
| `API_QUOTA_TOKEN_MAX` | `600` | Requests per token per window. |
| `API_QUOTA_TOKEN_WINDOW` | `60` | Token window in seconds. |
| `API_QUOTA_ORG_MAX` | `1200` | Requests per organization per family per window. |
| `API_QUOTA_ORG_WINDOW` | `60` | Organization window in seconds. |
| `RATE_LIMIT_MAX_KEYS` | `10000` | Hard cap on tracked limiter keys. |

Set a maximum or window to `0` to disable that dimension. The limiter is
process-local; multi-worker deployments should back it with a shared store.
Its memory is bounded: when `RATE_LIMIT_MAX_KEYS` is reached, the oldest
inserted key is evicted, so rotating IPs, tokens, or organization ids cannot
grow the state without bound.

## Response headers

Every successful response from a quota-protected endpoint carries:

| Header | Meaning |
| ------ | ------- |
| `X-RateLimit-Limit` | Maximum requests allowed in the active window. |
| `X-RateLimit-Remaining` | Requests left before the limit is hit. |
| `X-RateLimit-Reset` | Seconds until the oldest hit expires. |

A denied request returns `429` with the same headers plus `Retry-After`, and the
stable [error catalog](api-errors.md) body:

```json
{
  "error": "Too many requests. Please try again later.",
  "kind": "rate_limited",
  "code": "rate_limited"
}
```

## Protected endpoints

- `GET /api/v1/projects`, `/api/v1/projects/<id>`
- `GET /api/v1/reviews`, `/api/v1/reviews/<id>`
- `GET /api/v1/usage`
- `GET /workspaces/api/workspaces`
- `GET /workspaces/api/workspaces/<id>/members`

Add `@api_quota("<bucket>")` (from `app/services/ratelimit.py`) to a new public
view to apply the same contract. Every route in one family should use the same
bucket name so per-family limits are shared consistently.

Other per-user and per-IP limiters in the same module (import, search, chat,
stream, analyze, export) return the same catalog `rate_limited` body and a
`Retry-After` header when denied.
