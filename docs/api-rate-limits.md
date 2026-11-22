# API rate limits and quotas

Public workspace endpoints enforce two independent sliding-window quotas:

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
| `API_QUOTA_ORG_MAX` | `1200` | Requests per organization per window. |
| `API_QUOTA_ORG_WINDOW` | `60` | Organization window in seconds. |

Set a maximum or window to `0` to disable that dimension. The limiter is
process-local; multi-worker deployments should back it with a shared store.

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

- `GET /workspaces/api/workspaces`
- `GET /workspaces/api/workspaces/<id>/members`

Add `@api_quota("<bucket>")` (from `app/services/ratelimit.py`) to a new public
view to apply the same contract.
