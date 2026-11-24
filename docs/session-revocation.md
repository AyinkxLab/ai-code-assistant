# Session revocation

Every successful sign-in creates a server-side session row and stores its
opaque token in the signed session cookie. A request carrying a token whose row
is missing or revoked is logged out immediately, so revocation takes effect on
the next request instead of waiting for the cookie to expire.

## User: sign out every device

```bash
GET  /auth/api/sessions
POST /auth/api/sessions/revoke-all
```

`GET` lists the account's active sessions (creation time, last-seen time, and a
bounded user-agent string; tokens are never returned). `POST revoke-all` revokes
the current session and every other active session for the account, then logs
the caller out of the current browser.

The response reports how many sessions were revoked:

```json
{"revoked": 3}
```

## Administrator: revoke an account

```bash
POST /admin/users/<user_id>/sessions/revoke-all
Content-Type: application/json

{"reason": "laptop lost"}
```

Administrator access is required. `reason` is mandatory: it is written to the
audit log together with the acting administrator and the affected account
(action `sessions_revoked`, `initiated_by: "admin"`). Tokens are never logged.
A missing or invalid reason returns the structured `invalid_input` error (400),
an unknown account returns `not_found` (404), and a non-administrator receives
`forbidden` (403).

## Scope and limits

- Sessions created outside the sign-in flow (for example an internal test helper
  that calls `login_user` directly) have no tracked token and are not affected
  by revocation.
- Revocation ends authentication only. It does not rotate API tokens or reset a
  password; use the API token management surface for those credentials.
- A disabled account is still dropped on its next request by the existing
  deprovisioning check, independent of tracked sessions.
