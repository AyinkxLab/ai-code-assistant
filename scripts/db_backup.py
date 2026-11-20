"""Create and checksum a PostgreSQL backup (issue #709).

``pg_dump`` writes a custom-format archive into ``--output-dir`` and a sibling
``.sha256`` file records the digest. The database password is passed to the
child process through ``PGPASSWORD`` and never appears in the command line,
output, or logs.

Exit codes: ``0`` success, ``1`` backup failed, ``2`` invalid arguments or
configuration. Failures are printed as one JSON object on stderr.

Usage::

    python scripts/db_backup.py
    python scripts/db_backup.py --output-dir /var/backups/aica --json
    python scripts/db_backup.py --database-url "$DATABASE_URL" --label nightly
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_OUTPUT_DIR = ROOT / "backups"


class BackupError(Exception):
    """A backup could not be created.

    ``exit_code`` distinguishes bad configuration (2) from a failed backup (1).
    """

    def __init__(self, message: str, *, code: str = "backup_failed", exit_code: int = 1) -> None:
        super().__init__(message)
        self.code = code
        self.exit_code = exit_code


@dataclass(frozen=True)
class DbTarget:
    host: str | None
    port: int | None
    user: str
    password: str
    dbname: str
    sslmode: str | None

    def describe(self) -> str:
        """Connection details without the password."""
        location = self.host or "localhost"
        if self.port:
            location = f"{location}:{self.port}"
        return f"{location}/{self.dbname}"


def parse_database_url(database_url: str) -> DbTarget:
    """Parse a PostgreSQL URL without exposing the password in errors."""
    if not database_url:
        raise BackupError(
            "DATABASE_URL is required (or pass --database-url).",
            code="invalid_config",
            exit_code=2,
        )
    parsed = urlsplit(database_url)
    if parsed.scheme not in {"postgresql", "postgres"}:
        raise BackupError(
            "DATABASE_URL must be a PostgreSQL URL (postgresql://...).",
            code="invalid_config",
            exit_code=2,
        )
    dbname = unquote(parsed.path.lstrip("/"))
    if not dbname:
        raise BackupError(
            "DATABASE_URL does not name a database.", code="invalid_config", exit_code=2
        )
    query = parse_qs(parsed.query)
    return DbTarget(
        host=parsed.hostname,
        port=parsed.port,
        user=unquote(parsed.username or ""),
        password=unquote(parsed.password or ""),
        dbname=dbname,
        sslmode=query.get("sslmode", [None])[0],
    )


def child_environment(target: DbTarget) -> dict[str, str]:
    env = {**os.environ}
    if target.password:
        env["PGPASSWORD"] = target.password
    if target.sslmode:
        env["PGSSLMODE"] = target.sslmode
    return env


def connection_args(target: DbTarget) -> list[str]:
    args = ["--username", target.user, "--dbname", target.dbname]
    if target.host:
        args.extend(["--host", target.host])
    if target.port:
        args.extend(["--port", str(target.port)])
    return args


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def checksum_path(backup: Path) -> Path:
    return backup.with_name(backup.name + ".sha256")


def write_checksum(backup: Path, digest: str) -> Path:
    path = checksum_path(backup)
    path.write_text(f"{digest}  {backup.name}\n", encoding="utf-8")
    return path


def read_checksum(path: Path) -> str:
    try:
        value = path.read_text(encoding="utf-8").split()[0]
    except (OSError, IndexError) as exc:
        raise BackupError(f"cannot read checksum file: {path.name}") from exc
    if not re.fullmatch(r"[0-9a-fA-F]{64}", value):
        raise BackupError(f"checksum file is malformed: {path.name}")
    return value.lower()


def sanitize_error(text: str, target: DbTarget) -> str:
    if target.password:
        text = text.replace(target.password, "***")
    # Avoid echoing a URL a driver might print back.
    return re.sub(r"postgres(?:ql)?://\S+", "postgresql://***", text)


def _safe_label(value: str) -> str:
    label = re.sub(r"[^A-Za-z0-9._-]+", "_", value).strip("._-")
    return label or "backup"


def _dump_command(target: DbTarget, pg_dump: str, destination: Path) -> list[str]:
    return [
        pg_dump,
        "--format=custom",
        "--no-owner",
        "--no-privileges",
        "--file",
        str(destination),
        *connection_args(target),
    ]


def create_backup(
    database_url: str,
    output_dir: Path,
    *,
    pg_dump: str = "pg_dump",
    label: str | None = None,
    runner=None,
    now: datetime | None = None,
) -> dict[str, Any]:
    """Run pg_dump and write a checksum beside the archive."""
    target = parse_database_url(database_url)
    output_dir = Path(output_dir)
    try:
        output_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise BackupError(f"cannot create output directory: {output_dir}") from exc

    run = runner or subprocess.run
    timestamp = (now or datetime.now(UTC)).strftime("%Y%m%dT%H%M%SZ")
    name = f"{_safe_label(label or target.dbname)}-{timestamp}.dump"
    destination = output_dir / name
    if destination.exists():
        raise BackupError(f"backup file already exists: {destination.name}")

    try:
        fd, temporary = tempfile.mkstemp(
            prefix=f".{destination.name}.", suffix=".partial", dir=output_dir
        )
        os.close(fd)
    except OSError as exc:
        raise BackupError(f"cannot create a temporary file in {output_dir}") from exc
    temporary_path = Path(temporary)
    try:
        completed = run(
            _dump_command(target, pg_dump, temporary_path),
            capture_output=True,
            text=True,
            env=child_environment(target),
        )
        if completed.returncode != 0:
            detail = sanitize_error((completed.stderr or "").strip(), target)
            raise BackupError(
                f"pg_dump failed with exit code {completed.returncode}"
                + (f": {detail[:1000]}" if detail else "")
            )
        temporary_path.replace(destination)
    except FileNotFoundError as exc:
        raise BackupError(f"pg_dump not found: {pg_dump}") from exc
    except OSError as exc:
        raise BackupError(f"cannot write backup: {destination.name}") from exc
    finally:
        temporary_path.unlink(missing_ok=True)

    try:
        digest = sha256_file(destination)
        checksum = write_checksum(destination, digest)
    except OSError as exc:
        raise BackupError(f"cannot checksum backup: {destination.name}") from exc
    return {
        "ok": True,
        "backup": str(destination),
        "checksum_file": str(checksum),
        "sha256": digest,
        "bytes": destination.stat().st_size,
        "bytes_human": f"{destination.stat().st_size / (1024 * 1024):.2f} MiB",
        "database": target.describe(),
        "created_at": timestamp,
    }


def _error(code: str, message: str) -> None:
    print(json.dumps({"ok": False, "error": {"code": code, "message": message}}), file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--database-url",
        default=os.getenv("DATABASE_URL", ""),
        help="PostgreSQL URL (default: DATABASE_URL).",
    )
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path(os.getenv("BACKUP_DIR", str(DEFAULT_OUTPUT_DIR))),
        help="Directory for the archive and .sha256 file.",
    )
    parser.add_argument(
        "--pg-dump",
        default=os.getenv("PG_DUMP", "pg_dump"),
        help="Path to the pg_dump executable.",
    )
    parser.add_argument("--label", help="Prefix for the archive name (default: database name).")
    parser.add_argument("--json", action="store_true", help="Emit one JSON document.")
    args = parser.parse_args(argv)

    try:
        result = create_backup(
            args.database_url,
            args.output_dir,
            pg_dump=args.pg_dump,
            label=args.label,
        )
    except BackupError as exc:
        _error(exc.code, str(exc))
        return exc.exit_code

    if args.json:
        print(json.dumps(result, indent=2))
    else:
        print(
            f"backup: {result['backup']} ({result['bytes_human']})\n"
            f"sha256: {result['sha256']}\n"
            f"checksum: {result['checksum_file']}"
        )
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

