# Enterprise migration: workspaces into organizations

For the companion translation of legacy role strings and the built-in role
backfill, see [role migration](role-migration.md).

This repository represents an organization with an existing workspace. The
workspace owner (`workspaces.user_id`) is the organization owner, so migrating
an existing deployment does not copy projects, conversations, members, or usage
rows into new tables. The only additive step is recording an explicit
organization admin membership for each workspace owner so the Phase 11 admin
console can list and authorize organization administrators.

Backfill is therefore a **metadata-only** migration:

- No workspace, project, conversation, usage, budget, or member row is changed.
- The unique `(workspace_id, user_id)` membership constraint is respected.
- The script is idempotent: a second run creates nothing.
- Rollback deletes only `admin` rows whose `user_id` equals the workspace owner.

## Before you start

1. Take a database backup using [the backup guide](backup-restore.md).
2. Run the dry run and record the counts.
3. Apply during a low-traffic window. The operation only inserts missing
   membership rows; it takes no long locks on project or usage tables.

## Dry run

```console
python scripts/organization_migration.py --dry-run
```

Expected output on a deployment with three workspaces, one already migrated:

```text
Organization migration (dry run)
Workspaces scanned: 3
Already organization-ready: 1
Organization admin memberships to create: 2
  would add admin: workspace=1 (Acme platform) owner=alice (1)
  would add admin: workspace=2 (Acme mobile) owner=alice (1)
No changes written. Re-run with --apply to write.
```

`--json` emits the same plan as one JSON document for change tickets and CI.

## Backfill

```console
python scripts/organization_migration.py --apply
```

Expected output:

```text
Created 2 organization admin membership(s). Scanned 3 workspace(s).
```

## Verification

```console
python scripts/organization_migration.py --verify
```

Expected output when every workspace has an active owner/admin membership:

```text
Organization verification passed for 3 workspaces.
```

A failure lists each workspace and reason and exits `1`. Workspaces with no
owner, or with a stale owner membership, are reported as skipped by the dry run
and are not modified automatically; investigate those rows manually.

Equivalent SQL check:

```sql
SELECT w.id
FROM workspaces w
LEFT JOIN workspace_members m
  ON m.workspace_id = w.id
 AND m.user_id = w.user_id
 AND m.status = 'active'
 AND m.role IN ('owner', 'admin')
WHERE m.id IS NULL;
```

## Rollback

```console
python scripts/organization_migration.py --rollback
```

Expected output:

```text
Removed 2 organization admin membership(s).
```

Rollback removes only active `admin` memberships for the workspace owner. It
does not delete workspaces, projects, usage rows, or any other membership, so it
is safe to run if the console rollout is paused.

## Tests

`tests/test_organization_migration.py` covers the dry run (no writes),
idempotent apply, verification, and rollback. Run it with:

```console
python -m pytest tests/test_organization_migration.py -o addopts="" -q
```
