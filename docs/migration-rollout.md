# Safe migration rollout

Production schema changes must be backwards compatible for at least one deploy,
so old and new application instances can run against the same database during a
rolling release. This page defines the patterns, the preflight check, the
operator sequence, and the rollback path. It is written for PostgreSQL 16, which
production requires.

## Rules

1. **Expand, then contract.** Add new structures and dual-write first. Remove old
   structures only after every instance runs code that no longer needs them.
2. **One migration, one intent.** Do not mix a table rewrite with unrelated
   changes; a failed step is easier to reason about on its own.
3. **Migrations run from a controlled job**, not from every app replica's start
   script. `docker-compose.yml` runs `flask db upgrade` before gunicorn, which is
   fine for a single instance but unsafe when several replicas or regions start
   together.
4. **Long locks are a production incident.** A blocked `ALTER TABLE` also blocks
   every query behind it in the lock queue, including reads.
5. **Downgrades are not a backup.** Test them, but prefer a forward fix.

## Expand/contract patterns

### Add a column

```python
# Expand: nullable, no default rewrite, old code ignores it.
def upgrade():
    op.add_column("projects", sa.Column("archived_at", sa.DateTime(timezone=True)))
```

Backfill in batches outside the migration (a job or SQL script), then add
constraints in a later migration. Adding a `NOT NULL` column with a server
default takes an `ACCESS EXCLUSIVE` lock and rewrites the table on PostgreSQL
before 11; even on 16 the lock can still hurt.

### Add a `NOT NULL` constraint safely

```python
# Later migration, after the backfill has filled every row.
def upgrade():
    op.execute(
        "ALTER TABLE projects ADD CONSTRAINT projects_status_not_null "
        "CHECK (status IS NOT NULL) NOT VALID"
    )
    op.execute("ALTER TABLE projects VALIDATE CONSTRAINT projects_status_not_null")
```

`VALIDATE CONSTRAINT` takes a weaker lock and does not block writes. A later
migration can replace the check with a real `NOT NULL` if desired.

### Create an index

```python
def upgrade():
    with op.get_context().autocommit_block():
        op.create_index(
            "ix_projects_archived_at",
            "projects",
            ["archived_at"],
            postgresql_concurrently=True,
        )
```

`CONCURRENTLY` cannot run inside a transaction. `autocommit_block()` ends the
migration transaction around the statement. A failed concurrent build leaves an
invalid index; drop it before retrying.

### Rename or change a type

Never rename in place while old code is running. Use the same four steps:

1. Add the new column (expand).
2. Dual-write in the application.
3. Backfill existing rows.
4. Switch reads, then drop the old column after the next deploy (contract).

Changing a type in place (`ALTER COLUMN ... TYPE`) can rewrite the table. A new
column plus a backfill is predictable and reversible.

## Preflight check

`scripts/migration_preflight.py` reads the migration source (no database
connection) and reports operations that commonly take long or exclusive locks:
non-concurrent indexes, `NOT NULL` columns with defaults, type changes,
constraint validation, raw `CREATE INDEX` SQL, duplicate revisions, and multiple
graph heads.

```bash
python scripts/migration_preflight.py
python scripts/migration_preflight.py --since <current-revision>
python scripts/migration_preflight.py --json > preflight.json
python scripts/migration_preflight.py --advisory   # report, always exit 0
```

`--since` checks only revisions after the one you pass (for example the output
of `flask db current`), while graph checks still cover every file. Without it,
historical migrations are reported too, which is noisy for long-lived projects.

| Exit code | Meaning |
| --- | --- |
| `0` | No findings (or `--advisory` with findings). |
| `1` | At least one finding; review before deploying. |
| `2` | The directory could not be read or a migration file did not parse. The error is printed as JSON on stderr. |

`--json` emits one document:

```json
{
  "ok": false,
  "advisory": false,
  "versions": "migrations/versions",
  "migration_count": 1,
  "findings": [
    {
      "code": "index_not_concurrent",
      "file": "0001_add_index.py",
      "line": 12,
      "operation": "create_index",
      "message": "Index is built while holding a write lock on the table.",
      "suggestion": "Use op.create_index(..., postgresql_concurrently=True) ..."
    }
  ],
  "limitations": ["Does not connect to a database: ..."]
}
```

### What it cannot see

The check is advisory, not a proof:

- It never connects to PostgreSQL, so it does not know table size, lock waits,
  or which revisions are actually applied. Use `flask db current` and database
  monitoring for pending state.
- It only understands direct `op.*` calls and literal SQL. Dynamic SQL, loops,
  data backfills, and `sa.text()` constructed at runtime are not analysed.
- It does not check whether `CONCURRENTLY` is wrapped in an autocommit block,
  only that the flag is present.
- A clean report does not mean a migration is fast. Benchmark on a production
  copy when the table is large.

On the current `main` tree the check reports pre-existing issues: the Phase 12
read-index migration creates indexes without `CONCURRENTLY`, and two migration
files share revision `c9e1f2a3b4d5`, which also leaves multiple heads. Fix those
separately before making this check blocking in CI.

## Operator sequence

### 1. Preflight

```bash
git fetch && git checkout <release-ref>
flask --app wsgi db current
CURRENT=$(flask --app wsgi db current | awk '{print $1}')
python scripts/migration_preflight.py --since "$CURRENT" --json > preflight.json
flask --app wsgi db heads
```

Resolve findings or record why each is acceptable. Confirm exactly one head and
that the current revision matches what you expect.

### 2. Back up

Take a database backup and verify it restores into an empty database. For the
scripted path see `scripts/db_backup.py` and `scripts/db_restore.py` (when
available); otherwise use `pg_dump -Fc` plus a checksum and an off-host copy.

Record the backup identifier, checksum, and the current revision in the change
ticket. Do not proceed without a restorable backup.

### 3. Deploy the migration once

Run the upgrade as a single one-off job with a short statement timeout:

```bash
PGOPTIONS="-c statement_timeout=30s" flask --app wsgi db upgrade
```

Do not let every replica start migration at once. If the migration is expected
to be long, split it into expand and contract releases.

### 4. Verify

```bash
flask --app wsgi db current
curl -fsS https://app.example.com/health
```

Then check the application: login, one representative read, one write, and the
feature that uses the changed schema. Watch database lock waits and error rates
for at least one deploy cycle.

### 5. Roll back

Choose the least destructive option that restores service:

| Situation | Action |
| --- | --- |
| New code is bad, schema change is additive | Roll back the app only. The old code ignores the new nullable columns. |
| Migration failed halfway | Stop the deploy, inspect `alembic_version`, and fix forward with a new migration. Do not blindly downgrade. |
| Downgrade is tested and compatible | `flask --app wsgi db downgrade -1`, then redeploy the previous app version. |
| Destructive change already ran | Restore the verified backup into a new database, point `DATABASE_URL` at it, and redeploy. Expect data loss since the backup. |
| Wrong data was written | Prefer a targeted repair script over a full restore. |

Never run `downgrade` while instances that depend on the new schema are still
serving traffic. With expand/contract, the previous release remains compatible,
so the normal rollback is just the app deploy.

## Migration review checklist

- [ ] One purpose, and a clear upgrade **and** downgrade path.
- [ ] No table rewrite, or it is explicitly planned with a maintenance window.
- [ ] Indexes use `CONCURRENTLY` and an autocommit block.
- [ ] `NOT NULL` constraints are added `NOT VALID` then validated.
- [ ] New columns are nullable or have a safe default for old code.
- [ ] Old columns are dropped only in a later release.
- [ ] Preflight is clean, or every finding has an owner and a reason.
- [ ] A verified backup exists and the restore path was tested.
- [ ] Migration runs once, not from every replica.
- [ ] Post-deploy checks cover the feature that uses the schema.

## What was not executed

The preflight tests use fixture migration files and do not run PostgreSQL. Lock
behaviour, rewrite times, and `CONCURRENTLY` semantics on a real table were not
measured here. Validate long migrations on a staging copy with production-like
data before relying on these estimates.
