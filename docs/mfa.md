# Multi-factor authentication (MFA)

The application supports TOTP multi-factor authentication (RFC 6238, SHA-1,
six digits, 30 second step) using any standard authenticator app. MFA is
implemented without third-party dependencies in `app/services/mfa.py`.

## Enrollment

1. Sign in and open `/auth/mfa/setup`.
2. Add the displayed `otpauth://` URI to an authenticator app, or enter the
   base32 secret manually.
3. Enter the current six-digit code to confirm enrollment.

Once confirmed, `User.mfa_enabled` is set and `User.mfa_secret` stores the
per-user secret. The secret is never serialized in API responses or rendered
outside the setup page.

After enrollment, each password login becomes a two-step flow:

1. `POST /auth/login` accepts the password and redirects to `/auth/mfa`.
2. `POST /auth/mfa` accepts the current TOTP code and completes the session.

Codes are accepted within one 30 second step of clock skew.

## Workspace policy

Workspace owners configure MFA through the collaboration settings endpoint
(`PUT /workspaces/api/workspaces/<id>/settings`):

| Setting | Effect |
| ------- | ------ |
| `require_mfa` | Every member of the workspace must have MFA enabled to sign in. |
| `mfa_privileged_only` | When `true`, `require_mfa` applies only to owners and contributors; viewers are exempt. |

Defaults are `false` for both fields, preserving the pre-MFA behavior. Policy is
evaluated at login: a user who belongs to a workspace that requires MFA but has
not enrolled is shown an error and is **not** given a session. A user may belong
to several workspaces; the requirement applies if any workspace with a matching
role requires MFA.

## Sensitive actions

MFA is enforced at the authentication boundary. Because a session cannot be
created until the TOTP challenge succeeds, every authenticated action
(including administrative views such as `/admin/actions` and
`/admin/access-review`) is protected by the MFA gate. Break-glass accounts
(below) are the only exception.

## Break-glass and recovery

The operator-controlled `MFA_BREAK_GLASS_EMAILS` setting is a comma-separated
list of email addresses that skip MFA enforcement. Use it for a small, documented
set of recovery accounts that are otherwise tightly controlled.

```text
MFA_BREAK_GLASS_EMAILS=ops@example.com,security@example.com
```

`MFA_ISSUER` sets the issuer label shown in authenticator apps (default
`AI Code Assistant`).

If a user loses their authenticator and is not on the break-glass list, an
operator should reset the account by clearing `User.mfa_enabled` and
`User.mfa_secret`, then have the user re-enroll. Removing an account from
`require_mfa` scope (for example, changing its workspace role or the workspace
policy) also restores access on the next login.
