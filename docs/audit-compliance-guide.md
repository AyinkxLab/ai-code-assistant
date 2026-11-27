# Audit and compliance guide

This guide is the entry point for auditors, customer security reviewers, and
operators. It explains what the product records, how long it is kept, how to
export it, and what the evidence pack does **not** certify. It links to the
detailed guides instead of restating them, so there is one source of truth per
topic.

| Topic | Detailed guide |
| --- | --- |
| Append-only log and hash chain | [Audit log integrity](audit-integrity.md) |
| Retention window, purge, legal hold | [Audit retention and legal hold](audit-retention.md) |
| Evidence pack contents and limits | [Compliance evidence pack](compliance-evidence.md) |
| Field-level export controls | [Data classification](data-classification.md) |
| Admin investigation view and export | [Admin action audit view](admin-actions.md) |

## What is audited

Sensitive and security-relevant actions append a row to `audit_logs`. Rows are
written through `app.services.audit.record` and are never updated or deleted by
the application. Examples include authentication success/failure, account
creation, conversation and prompt deletion, budget and pricing changes, audit
exports, user data-export lifecycle events, SIEM endpoint changes, and domain
verification/SSO enforcement changes.

Each row records:

| Field | Meaning |
| --- | --- |
| `workspace_id` | Workspace whose hash chain the row belongs to; `NULL` is the global chain. |
| `user_id` | Acting account, or `NULL` for an unauthenticated event such as a failed login. |
| `action` | Stable action name, for example `login_success`. |
| `target_type`, `target_id` | The affected object, when there is one. |
| `metadata` | Small contextual JSON blob. It is hashed but is not part of public exports. |
| `prev_hash`, `record_hash` | SHA-256 chain link used by `flask audit verify`. |
| `created_at` | UTC timestamp. |

The action list is not a complete list of every database write. It covers
security-relevant and administrative operations, not routine content reads.
Request bodies, prompts, message content, and secrets are not written to the
audit log.

### Integrity check

```bash
flask audit verify
flask audit verify --workspace-id 7 --json
```

Exit status is `0` for intact chains, `1` when a modified or missing row is
found, and `2` for invalid input. The chain detects edits and deletions of
middle rows. It cannot prove that the newest rows were not truncated; keep
database backups or publish the latest `record_hash` externally if that matters
for your threat model. Legacy rows with `NULL` hashes are reported as
`unhashed`, never as tampered. Full semantics are in
[audit log integrity](audit-integrity.md).

## Retention

Retention is per workspace and lives in `audit_retention_policies`. A workspace
with no policy uses **365 days** and no legal hold. The accepted window is
**1 to 3650 days**.

```bash
flask audit retention show --workspace-id 7
flask audit retention set --workspace-id 7 --days 90
flask audit retention set --workspace-id 7 --legal-hold
flask audit retention set --workspace-id 7 --no-legal-hold
flask audit purge --workspace-id 7 --dry-run
flask audit purge --workspace-id 7
```

`flask audit purge` deletes audit rows **strictly older** than
`now - retention_days`; a row exactly at the cutoff is retained. The purge only
touches that workspace's audit rows. Global rows (`workspace_id IS NULL`) are
not removed by a workspace policy. Run the purge from a scheduler (cron, a
Kubernetes CronJob, or your platform scheduler) and use `--dry-run` after
changing a window. Details: [audit retention and legal hold](audit-retention.md).

### Legal hold and deletion

A legal hold always wins over age: while it is active, `flask audit purge`
deletes nothing and reports `skipped_reason: "legal_hold"`. Releasing it with
`--no-legal-hold` lets the next scheduled run proceed. Holds are per workspace
and do not expire automatically.

Deletion behaviour follows the same boundaries as the rest of the data model:

- Deleting a **workspace** cascades to its audit rows and retention policy.
- Deleting a **user account** sets `user_id` to `NULL` on retained audit rows,
  preserving the historical action without the identity link. `user_id` is
  deliberately excluded from the hash, so this does not make the chain look
  tampered.
- A legal hold blocks **audit retention purge only**. It is not a general
  account-deletion or data-export control; use the user data export workflow
  ([user data export](user-data-export.md)) for a copy of account data.

## Exporting evidence

### Audit export endpoint

`GET /admin/audit/export` streams the audit rows an administrator is allowed to
see. It requires an authenticated admin; non-admins receive
`403 forbidden` in the shared [error catalog](api-errors.md) shape.

| Query parameter | Meaning |
| --- | --- |
| `format` | `json` (default) or `csv`. |
| `actor` | Exact username. Unknown names return no rows. |
| `action` | Exact action name. |
| `resource_type`, `resource_id` | Exact target type and id. |
| `start`, `end` | Inclusive `YYYY-MM-DD` dates. |
| `limit` | Maximum rows, 1-50000 (default 10000). |

The projection is fixed at the `internal` classification level: actor
usernames, the metadata blob, secrets, and prompt content are never exported.
Every export appends an `audit_exported` entry for the admin who ran it. Use
the investigation view at `/admin/actions` when you need to browse rather than
download; `/admin/actions?format=json` returns the same whitelisted rows.

### Workspace audit API

Owners can read the defined workspace audit event subset at
`GET /workspaces/api/workspaces/<workspace_id>/audit`. It supports `event_type` and
`actor` filters and page/per-page pagination, and requires the owner-level
`view_audit` capability. Non-members receive a uniform `404`; members without
the capability receive `403`.

#### Role-change events

Every role assignment, change, and removal writes `event_type: "role.changed"`
through `app.services.activity.record_role_change`. Failed or unauthorized
mutations write nothing. The event metadata always carries:

| Field | Meaning |
| --- | --- |
| `action` | `assigned`, `changed`, or `removed`. |
| `old_role`, `new_role` | Before/after role; `null` means no role on that side. |
| `actor_id`, `actor_username` | Who performed the mutation. |
| `target_user_id`, `target_username` | Whose role changed. |
| `membership_id` | Membership row when one exists. |
| `request_id` | Correlation id from `X-Request-ID`, or a generated id. |
| `reason` | Present only when the caller supplied one. |

The same transition is emitted as a structured `role.changed` record by
`app.services.chat_audit`, with `request_id` added automatically. These events
live in the workspace audit view and the structured log; they are distinct from
the append-only `audit_logs` hash chain described above. The audit page renders
the role transition, target, and request id for owners.

### Evidence pack

```python
from app.services.evidence_pack import build_evidence_pack

pack = build_evidence_pack(workspace_id, actor=current_user)
```

The pack is a bounded JSON document containing the audit export and chain
verification result, an access review, the effective retention/legal-hold
policy, and non-secret security switches. Collections are capped and carry a
`truncated` flag. Fields are projected through the
[data classification](data-classification.md) helper at `internal` level, so
usernames, emails, metadata blobs, and secrets are excluded.

An actor without the owner-level `view_audit` capability gets
`PermissionError`; an unknown workspace gets `LookupError`. The operator path
(`actor=None`, for example a CLI maintenance job) relies on deployment
credentials instead, the same trust model as the other `flask audit` commands.

## What the evidence pack does and does not certify

The pack is **evidence, not a certification**. The payload states this
explicitly with `"certification": false` and the disclaimer:

> This pack is evidence of current configuration and recent activity. It is
> not a certification, attestation, or audit opinion.

It **does** show the current configuration and recent activity at the time it
was generated, and it records whether the audit chains verified at that moment.

It **does not** certify that any framework (SOC 2, ISO 27001, GDPR, and so on)
is met, and it is not an audit opinion or an attestation. It does not
reconstruct historical state, prove that logs were never deleted outside the
application, or replace evidence from the hosting platform, identity provider,
or database backups. See [compliance evidence pack](compliance-evidence.md) for
the control-by-control contents and limits.

## Reproduction checklist

A reviewer who wants to reproduce the evidence can:

1. Run `flask audit verify --json` and keep the output.
2. Run `flask audit retention show --workspace-id <id> --json`.
3. Download `GET /admin/audit/export?format=json&start=...&end=...` with an
   admin session and keep the file plus the `X-Export-Limit` header.
4. Call `build_evidence_pack(<id>)` in a maintenance shell (or as the workspace
   owner) and keep the JSON output.
5. Record the date, deployment revision, and who ran each command. The pack
   describes current state; a review that needs history must retain these
   exports over time.
