# GitLab integration

Workspace owners can connect a GitLab account with a scoped OAuth token and list
their repositories (#677).

## Configuration

| Setting | Purpose |
| ------- | ------- |
| `GITLAB_CLIENT_ID` / `GITLAB_CLIENT_SECRET` | OAuth application credentials |
| `GITLAB_REDIRECT_URI` | Must match the GitLab application's callback URL |
| `GITLAB_SCOPES` | Defaults to `read_api` |
| `GITLAB_BASE_URL`, `GITLAB_API_URL` | API host (defaults to gitlab.com) |
| `GITLAB_OAUTH_AUTHORIZE_URL` / `GITLAB_OAUTH_TOKEN_URL` / `GITLAB_OAUTH_REVOKE_URL` | OAuth endpoints |
| `GITLAB_REQUEST_TIMEOUT` | Per-request timeout in seconds |
| `GITLAB_MAX_REPOSITORIES` | Upper bound on listed repositories |

## Flow

1. `GET /gitlab/workspaces/<workspace_id>/connect` (owner only) stores a random
   `state` in the session and redirects to GitLab.
2. `GET /gitlab/callback?state=...&code=...` validates the state, exchanges the
   code, fetches the GitLab profile, and stores the access token.

The token is encrypted with AES-256-GCM through
`integration_credentials` (`integration='gitlab'`, `name='access_token'`), never
written in plaintext and never returned by an API. `GitlabAccount` stores only
metadata (username, display name, scopes, revocation time).

## Endpoints

| Method | Path | Purpose |
| ------ | ---- | ------- |
| `GET` | `/gitlab/workspaces/<workspace_id>/connect` | Start OAuth |
| `GET` | `/gitlab/callback` | Complete OAuth |
| `GET` | `/gitlab/api/workspaces/<workspace_id>/repositories` | List repositories |
| `POST` | `/gitlab/api/workspaces/<workspace_id>/revoke` | Revoke and delete the token |

All routes are owner-scoped (`404` otherwise). Repository listing is bounded by
`per_page`, `max_pages`, and `GITLAB_MAX_REPOSITORIES`.

## Errors

Errors are structured `{ "error": ..., "code": ... }`:

- `invalid_oauth_state` (400) — missing or mismatched state.
- `gitlab_not_configured` (503) — client id/secret unset.
- `gitlab_auth_failed` (400/401) — GitLab rejected the code or token.
- `gitlab_api_error` (502) — transport or unexpected GitLab response.

Error messages never include the access token.
