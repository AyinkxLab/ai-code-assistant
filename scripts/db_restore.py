"""Verify and restore a PostgreSQL backup (issue #709).

The restore refuses a database that already contains tables unless ``--force``
is given. The archive checksum is verified first; a mismatch stops the restore
before anything connects to the database.

Exit codes: ``0`` success, ``1`` checksum/restore/database failure, ``2``
invalid arguments or configuration. Failures are printed as one JSON object on
stderr and never include the database password.

Usage::

    python scripts/db_restore.py --backup backups/aica-20260101T000000Z.dump
    python scripts/db_restore.py --backup archive.dump --checksum archive.dump.sha256
    python scripts/db_restore.py --backup archive.dump --force   # destructive
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:  # pragma: no cover - direct script execution
    sys.path.insert(0, str(ROOT))

import psycopg2  # noqa: E402

from scripts.db_backup import (  # noqa: E402
    BackupError,
    DbTarget,
    checksum_path,
    child_environment,
    connection_args,
    parse_database_url,
    read_checksum,
    sanitize_error,
    sha256_file,
)

_connect = psycopg2.connect

_EMPTY_DB_SQL = """
SELECT table_schema || '.' || table_name
FROM information_schema.tables
WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
ORDER BY 1
LIMIT 20
"""


class RestoreError(Exception):
    """A restore could not proceed."""

    def __init__(self, message: str, *, code: str, exit_code: int = 1) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


def verify_checksum(backup: Path, checksum: Path | None = None) -> str:
    """Return the verified digest or raise when it cannot be trusted."""
    if not backup.is_file():
        raise RestoreError(
            f"backup file not found: {backup.name}", code="invalid_config", exit_code=2
        )
    checksum = checksum or checksum_path(backup)
    if not checksum.is_file():
        raise RestoreError(
            f"checksum file not found: {checksum.name}",
            code="checksum_missing",
            exit_code=1,
        )
    try:
        expected = read_checksum(checksum)
        actual = sha256_file(backup)
    except BackupError as exc:
        raise RestoreError(str(exc), code="checksum_invalid", exit_code=1) from exc
    except OSError as exc:
        raise RestoreError(
            f"cannot read backup or checksum: {type(exc).__name__}",
            code="backup_unreadable",
            exit_code=1,
        ) from exc
    if actual != expected:
        raise RestoreError(
            "checksum mismatch: the backup file does not match its .sha256 file",
            code="checksum_mismatch",
            exit_code=1,
        )
    return actual


def database_is_empty(target: DbTarget, *, connect=None) -> tuple[bool, list[str]]:
    """Return ``(is_empty, sample_table_names)`` for the target database."""
    connector = connect or _connect
    kwargs: dict[str, Any] = {
        "host": target.host,
        "port": target.port,
        "user": target.user,
        "password": target.password,
        "dbname": target.dbname,
        "connect_timeout": 10,
    }
    if target.sslmode:
        kwargs["sslmode"] = target.sslmode
    if not target.host:
        kwargs.pop("host")

    try:
        connection = connector(**kwargs)
    except Exception as exc:  # driver errors vary by version
        raise RestoreError(
            f"cannot connect to {target.describe()}: {type(exc).__name__}",
            code="database_unreachable",
            exit_code=1,
        ) from exc

    try:
        cursor = connection.cursor()
        try:
            cursor.execute(_EMPTY_DB_SQL)
            tables = [row[0] for row in cursor.fetchall()]
        finally:
            cursor.close()
    except Exception as exc:
        raise RestoreError(
            f"cannot inspect {target.describe()}: {type(exc).__name__}",
            code="database_check_failed",
            exit_code=1,
        ) from exc
    finally:
        connection.close()
    return not tables, tables


def _restore_command(target: DbTarget, pg_restore: str, backup: Path, force: bool) -> list[str]:
    command = [pg_restore, "--no-owner", "--no-privileges", *connection_args(target)]
    if force:
        command.extend(["--clean", "--if-exists"])
    command.append(str(backup))
    return command


def restore_backup(
    database_url: str,
    backup: Path,
    *,
    checksum: Path | None = None,
    force: bool = False,
    pg_restore: str = "pg_restore",
    runner=None,
    connect=None,
) -> dict[str, Any]:
    """Verify the archive and restore it into an empty database."""
    target = parse_database_url(database_url)
    backup = Path(backup)
    digest = verify_checksum(backup, checksum)

    is_empty, tables = database_is_empty(target, connect=connect)
    if not is_empty and not force:
        raise RestoreError(
            "database is not empty; refusing to overwrite it without --force "
            f"(found {len(tables)} table(s), e.g. {', '.join(tables[:5])})",
            code="database_not_empty",
            exit_code=1,
        )

    run = runner or subprocess.run
    try:
        completed = run(
            _restore_command(target, pg_restore, backup, force),
            capture_output=True,
            text=True,
            env=child_environment(target),
        )
    except FileNotFoundError as exc:
        raise RestoreError(
            f"pg_restore not found: {pg_restore}", code="pg_restore_missing"
        ) from exc
    except PermissionError as exc:
        raise RestoreError(
            f"permission denied while running pg_restore: {exc}",
            code="permission_denied",
        ) from exc
    except OSError as exc:
        raise RestoreError(
            f"cannot run pg_restore: {type(exc).__name__}", code="restore_failed"
        ) from exc

    if completed.returncode != 0:
        detail = sanitize_error((completed.stderr or "").strip(), target)
        raise RestoreError(
            f"pg_restore failed with exit code {completed.returncode}"
            + (f": {detail[:1000]}" if detail else ""),
            code="restore_failed",
        )

    return {
        "ok": True,
        "backup": str(backup),
        "sha256": digest,
        "database": target.describe(),
        "forced": force,
    }


def _error(code: str, message: str) -> None:
    print(json.dumps({"ok": False, "error": {"code": code, "message": message}}), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", ""),
        help="Target PostgreSQL URL (default: DATABASE_URL).",
    )
    parser.add_argument("--backup", type=Path, help="Archive produced by db_backup.py.")
    parser.add_argument("--checksum", type=Path, help="Checksum file (default: <backup>.sha256).")
    parser.add_argument(
        "--pg-restore",
        default=os.getenv("PG_RESTORE", "pg_restore"),
        help="Path to the pg_restore executable.",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Run pg_restore --clean against a non-empty database (destructive).",
    )
    parser.add_argument("--json", action="store_true", help="Emit one JSON document.")
    args = parser.parse_args(argv)

    if args.backup is None:
        _error("invalid_config", "--backup is required.")
        return 2

    try:
        result = restore_backup(
            args.database_url,
            args.backup,
            checksum=args.checksum,
            force=args.force,
            pg_restore=args.pg_restore,
        )
    except BackupError as exc:
        _error(exc.code, str(exc))
        return exc.exit_code
    except RestoreError as exc:
        _error(exc.code, str(exc))
        return exc.exit_code

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        note = " (forced clean restore)" if result["forced"] else ""
        print(
            f"restored: {result['backup']}\n"
            f"into: {result['database']}{note}\n"
            f"sha256: {result['sha256']}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
