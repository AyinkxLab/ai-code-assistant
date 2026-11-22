# User data deletion and anonymization

The deletion workflow lets an account owner ask for their personal data to be
removed or their identity to be anonymized. It follows the same shape as the
[user data export](user-data-export.md) workflow: explicit request states,
owner-only access, a per-user rate limit, structured errors, and an audit entry
for every transition.

The implementation is `app/services/data_deletion.py`; requests live in
`user_deletion_requests` (`app/models/user_deletion_request.py`).

## States

```
requested -> confirmed -> completed
     \________/
          cancelled
```

| State | Meaning |
| --- | --- |
| `requested` | The account owner asked for deletion or anonymization. Nothing has changed yet. |
| `confirmed` | The owner submitted the exact confirmation phrase. The request is applied in the same transaction, so this state is only observable mid-transaction. |
| `completed` | Content was deleted (deletion mode) and the profile was anonymized. The account is disabled. |
| `cancelled` | The owner cancelled before confirmation. No account data changed. |

A second request is refused while one is `requested` or `confirmed`, and an
account that has already completed a request is refused with `conflict`.

## Modes

| Mode | What happens |
| --- | --- |
| `deletion` (default) | Delete personal content rows, then anonymize the profile. |
| `anonymization` | Anonymize the profile only; personal content is retained. |

Use `anonymization` when an organization must keep workspace content (for
example project history) but the person must no longer be identifiable.

## API

All routes require a signed-in session and only ever act on the caller's own
requests.

| Route | Purpose |
| --- | --- |
| `GET /auth/api/data-deletion` | List the caller's requests. |
| `POST /auth/api/data-deletion` | Create a request. Optional body: `{"mode": "deletion"}` or `{"mode": "anonymization"}`. |
| `GET /auth/api/data-deletion/<id>` | Read one request. |
| `POST /auth/api/data-deletion/<id>/confirm` | Confirm and apply. Body: `{"confirmation": "delete my data"}`. |
| `POST /auth/api/data-deletion/<id>/cancel` | Cancel a pending request. |

The confirmation phrase is exactly `delete my data` (case-insensitive,
surrounding whitespace ignored). A missing or different phrase returns
`invalid_input` and changes nothing. A successful confirmation ends the
session and returns the completed request plus a summary of removed rows.

Errors use [the API error catalog](api-errors.md):

| Situation | HTTP | Code |
| --- | --- | --- |
| Missing/wrong confirmation phrase, unknown mode | 400 | `invalid_input` |
| Request belongs to another user, or does not exist | 404 | `not_found` |
| Pending request already exists, request already completed/cancelled | 409 | `conflict` |
| More than three requests in 24 hours | 429 | `rate_limited` |

## What is deleted, anonymized, and retained

### Deleted (`deletion` mode only)

- Conversations owned by the user, including their messages, attachments, and
  shares.
- Prompts owned by the user and their prompt-version history.
- Provider API keys (encrypted values are removed with the row).
- GitHub account connections (encrypted OAuth tokens are removed with the row).
- Pending/completed export request rows, so one-time download links cannot be
  reused.

### Anonymized (both modes)

| Field | After completion |
| --- | --- |
| `users.username` | `deleted-user-<id>` |
| `users.email` | `deleted-<id>@deleted.invalid` |
| `users.password_hash` | Random, unusable hash |
| `users.is_active` | `false` |
| `users.mfa_secret` / `mfa_enabled` | `NULL` / `false` |
| `users.stellar_network` | `NULL` |
| `users.last_login_at` | `NULL` |
| Active workspace memberships | Soft-removed (`status=removed`, `removed_at` set) |

### Retained

- The `users` row itself (disabled and anonymized). It is not deleted because
  audit rows reference it and because deleting it would cascade-delete owned
  workspaces and their audit chains.
- Every `audit_logs` row. Audit entries keep their `user_id` pointer to the
  now-anonymized account and are never edited or deleted.
- Workspaces owned by the account. Ownership is not transferred automatically;
  an organization should transfer ownership before the owner deletes their
  account if the workspace must stay administrable.
- Workspace-scoped records such as projects, reviews, integration credentials,
  and usage/cost rows. These belong to the organization, not the person.
- Prompt-version rows changed by others and not belonging to the user's prompts.

## Audit

Each transition appends an immutable audit entry:

- `user_deletion_requested`
- `user_deletion_confirmed`
- `user_deletion_cancelled`
- `user_deletion_completed`

The completion entry records the mode and the per-table deletion counts. No
personal content, email address, or password material is written to audit
metadata.

## Tests

`tests/test_data_deletion.py` covers the happy path, a missing confirmation
phrase, foreign-user access, an already-deleted account, cancellation, rate
limiting, audit preservation, and anonymization-only mode.
