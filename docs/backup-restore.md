# Backup and restore

`scripts/db_backup.py` and `scripts/db_restore.py` wrap PostgreSQL's
`pg_dump`/`pg_restore` with checksums and a safety check. They intentionally do
not reimplement PostgreSQL backup logic or store credentials.

## What the scripts do

| Script | Behaviour |
| --- | --- |
| `scripts/db_backup.py` | Runs `pg_dump --format=custom`, writes `<label>-<UTC timestamp>.dump`, then writes a sibling `.sha256` file. |
| `scripts/db_restore.py` | Verifies the archive against its checksum, refuses a non-empty database unless `--force`, then runs `pg_restore`. |

The database password is passed to the child process through `PGPASSWORD`; it
never appears in the command line, success output, or error output. Error
details are sanitized before printing.

## Creating a backup

```bash
export DATABASE_URL=postgresql://aica:secret@db.example.net:5432/aica
python scripts/db_backup.py --output-dir /var/backups/aica --label nightly
```

Flags:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--database-url` | `DATABASE_URL` | Source database. Must be a `postgresql://` URL. |
| `--output-dir` | `$BACKUP_DIR` or `./backups` | Directory for the archive and checksum. Created if missing. |
| `--label` | database name | Prefix for the file name; unsafe characters are replaced. |
| `--pg-dump` | `$PG_DUMP` or `pg_dump` | Explicit binary path for version-matched tooling. |
| `--json` | off | One JSON document on stdout. |

Output files:

```text
aica-nightly-20260101T000000Z.dump
aica-nightly-20260101T000000Z.dump.sha256
```

The checksum file contains `<sha256>  <filename>`, the same shape as
`sha256sum`. It detects corruption or a truncated copy; it does **not** prove the
archive came from a trusted source (use signatures or object-lock controls for
that).

Exit codes: `0` success, `1` `pg_dump` failed or the archive could not be
written/checksummed, `2` invalid arguments or configuration. Errors are a single
JSON object on stderr, for example:

```json
{"ok": false, "error": {"code": "invalid_config", "message": "DATABASE_URL must be a PostgreSQL URL (postgresql://...)."}}
```

## Restoring

```bash
# The target database must exist and be empty (no user tables).
export DATABASE_URL=postgresql://aica:secret@new-db.example.net:5432/aica
python scripts/db_restore.py --backup /var/backups/aica/aica-nightly-20260101T000000Z.dump
```

The script checks, in order:

1. `--backup` exists.
2. A checksum file exists and matches the archive.
3. The target database has no tables outside `pg_catalog` /
   `information_schema`.

If the database is not empty, the restore stops before invoking `pg_restore`.
`--force` is the only override and runs `pg_restore --clean --if-exists`, which
drops existing objects. Treat it as destructive: it can remove data that is not
in the archive.

Flags:

| Flag | Default | Purpose |
| --- | --- | --- |
| `--database-url` | `DATABASE_URL` | Restore target. |
| `--backup` | required | Archive from `db_backup.py`. |
| `--checksum` | `<backup>.sha256` | Checksum file to verify. |
| `--force` | off | Allow a non-empty target and clean existing objects. |
| `--pg-restore` | `$PG_RESTORE` or `pg_restore` | Explicit binary path. |
| `--json` | off | One JSON document on stdout. |

Exit codes: `0` success, `1` checksum mismatch/missing checksum/restore failure/
non-empty database/permission failure, `2` invalid arguments or configuration.
Structured error codes include `checksum_mismatch`, `checksum_missing`,
`database_not_empty`, `database_unreachable`, `permission_denied`,
`pg_restore_missing`, and `restore_failed`.

### Post-restore verification

```bash
flask --app wsgi db current
psql "$DATABASE_URL" -c "select count(*) from users;"
curl -fsS https://app.example.com/health
```

Then log in, open a conversation, and run one write path. Check that extensions
(for example `pgcrypto`, if your installation uses one) and application roles
exist; `pg_restore` does not create roles.

## Retention expectations

Retention is a policy decision; the scripts do not delete anything. A common
starting point:

| Tier | Keep | Example |
| --- | --- | --- |
| Hourly | 24–48 hours | Fast recovery from a bad deploy |
| Daily | 14–30 days | Normal operational restore |
| Weekly | 8–12 weeks | Slower-moving corruption |
| Monthly | 12 months | Audit/legal requirements |

Additional expectations:

- Keep at least one copy outside the primary region and outside the database
  provider's failure domain.
- Use object storage versioning or immutable retention (object lock) so a
  compromised operator cannot delete history.
- Record the backup identifier, checksum, and Alembic revision with each release.
- Test a restore into an empty database at least monthly, not only when an
  incident happens.
- Alert when a scheduled backup does not produce both the archive and the
  checksum, or when the archive size drops sharply.

## Encryption expectations

Backups contain user data, password hashes, and encrypted provider API keys:

- Encrypt archives at rest with the storage provider's KMS-managed encryption
  (or `age`/GPG before upload).
- Restrict read access to the backup bucket and the backup host.
- Encrypt in transit (`pg_dump` to local disk is local; uploads must use TLS).
- Back up `SECRET_KEY` separately in the secret manager. Stored provider keys are
  encrypted with a key derived from `SECRET_KEY`; a database restore without the
  matching secret leaves those values undecryptable.
- Do not put passwords in shell history. Prefer the environment/secret manager,
  as in the examples above.
- A checksum is integrity, not confidentiality or authenticity. Add signing if
  you need to prove provenance.

## Scheduling example

```cron
15 2 * * * cd /srv/aica && /srv/aica/.venv/bin/python scripts/db_backup.py \
  --output-dir /var/backups/aica --label nightly >> /var/log/aica-backup.log 2>&1
```

Rotate and upload the files with your normal retention tooling. The scripts exit
non-zero on failure, so a scheduler can alert on the exit code.

## What was not executed here

The tests mock `pg_dump`, `pg_restore`, and the database connection; no real
PostgreSQL server or binary was used. Not verified in this change:

- behaviour across `pg_dump`/`pg_restore` major-version mismatches;
- roles, ownership, extensions, tablespaces, and large-object handling in a real
  cluster;
- restore duration and lock impact on a production-sized database;
- KMS/object-lock configuration at a specific cloud provider.

Run a restore drill in staging with a production-like backup before relying on
this procedure.
