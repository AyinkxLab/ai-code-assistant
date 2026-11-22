# Audit retention and legal hold

Workspaces control how long their audit rows (`audit_logs`) are kept. The
policy lives in `audit_retention_policies`; a workspace with no row uses the
default window (365 days) with no legal hold.

## Setting the policy

```bash
flask audit retention show --workspace-id 7
flask audit retention set --workspace-id 7 --days 90
flask audit retention set --workspace-id 7 --legal-hold
flask audit retention set --workspace-id 7 --no-legal-hold
```

`--days` must be an integer from 1 to 3650. Booleans, strings, and out-of-range
values are rejected with exit status `2` and a structured error:

```json
{"error": "retention_days must be between 1 and 3650.", "code": "invalid_input"}
```

Only a workspace owner (`manage_settings` capability) may change the policy or
run a purge from application code. The CLI is an operator tool: it runs with the
deployment's database credentials and does not impersonate an owner.

## Scheduling the purge

Run the purge from cron, a Kubernetes CronJob, or the platform scheduler:

```bash
flask audit purge --workspace-id 7
flask audit purge --workspace-id 7 --dry-run   # report only
```

The job deletes rows **strictly older** than the cutoff (`now - retention_days`);
a row exactly at the cutoff is retained. Use `--dry-run` first when changing a
window. The JSON form reports `deleted`, `retention_days`, `cutoff`, and
`skipped_reason`.

## Legal hold

A legal hold always wins. While it is active, `flask audit purge` deletes
nothing and returns `skipped_reason: "legal_hold"`; releasing the hold with
`--no-legal-hold` lets the next run proceed. Holds are per workspace and are
not expired automatically.

## Effect on the hash chain

Purging removes a contiguous oldest prefix. The earliest surviving row is
treated as a chain anchor, so `flask audit verify` still succeeds; a deleted
middle row is still detected. Global rows (`workspace_id IS NULL`) are not
removed by a workspace policy. See
[audit log integrity](audit-integrity.md) for the full chain semantics.
