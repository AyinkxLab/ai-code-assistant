# Role migration and default-role backfill

Phase 11 replaces free-form legacy membership strings with the canonical,
capability-backed role names used by [the organization console](organization-admin.md).
The migration is additive, idempotent, and reversible.

## What changes

* Known aliases are translated without widening access:
  `reader`/`guest` → `viewer`, `member`/`editor` → `contributor`,
  `administrator`/`org_admin` → `admin`, `workspace_owner` → `owner`.
* Unknown strings are **never guessed**. They are reported as
  `unknown_legacy_role` and left untouched.
* Duplicate active memberships for the same workspace/user are resolved by
  choosing one known, active, highest-ranked row. Extra active rows become
  `removed`; unknown rows are never promoted to `owner` or `admin`.
* Four built-in role definitions (`viewer`, `contributor`, `admin`, `owner`) are
  backfilled once per organization owner into `organization_roles`. The
  organization `admin` definition intentionally has no workspace capabilities.

Every change is recorded in `role_migration_records` with exact before/after
snapshots, so rollback restores the old values instead of applying a second
heuristic.

## Dry run

```console
python scripts/role_migration.py --dry-run
```

Example output:

```text
Role migration (dry run)
Workspaces scanned: 2
Memberships scanned: 3
Role/status changes to apply: 2
Duplicate legacy groups: 1
Unknown legacy roles skipped: 1
Default built-in roles to backfill: 8
  would change: workspace=1 user=3 row=4 reader/active -> viewer/active (legacy_alias)
  duplicate: workspace=1 user=3 roles=['administrator', 'viewer'] keep=admin
  skipped unknown: workspace=2 user=7 role='wizard'
  would add built-in role: owner=1 role=admin
No changes written. Re-run with --apply to write.
```

`--json` emits the same plan as one machine-readable document. `--owner-id <id>`
restricts scanning to one organization owner.

## Apply, verify, rollback

```console
python scripts/role_migration.py --apply
python scripts/role_migration.py --verify
python scripts/role_migration.py --rollback
```

Apply is idempotent: a second run creates no rows and reports no changes.
Verification fails with exit code `1` when a legacy alias, an active duplicate,
or a missing built-in role remains. Rollback restores every recorded membership
snapshot and deletes only the built-in rows created by the migration. It does
not delete workspaces, projects, custom roles, or members that were already
`removed` before the migration.

## Structured errors

Invalid input exits `2` with one JSON document on stderr:

```json
{"error": {"code": "invalid_owner_id", "message": "owner-id must be a positive integer.", "details": {"owner_id": "nope"}}}
```

Unexpected database errors use code `role_migration_error` and never include a
raw exception message in the public payload.

## Database migration

The Alembic revision `d6e7f8a9b0c1` creates `organization_roles` and
`role_migration_records`, then seeds built-in roles for owners that already
exist. It is a single linear head after `b4e2f1a3c5d7`; downgrade drops only
these two tables. Owners created after deployment are covered by rerunning
`--apply`.

## Tests

`tests/test_role_migration.py` covers the dry run, unknown aliases, duplicate
resolution, idempotency, verification, rollback, structured errors, and the
Alembic upgrade/downgrade path:

```console
python -m pytest tests/test_role_migration.py -o addopts="" -q
```
