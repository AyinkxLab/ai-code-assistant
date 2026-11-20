"""Advisory preflight for Alembic migrations (issue #710).

Walks ``migrations/versions`` (or ``--versions``) without connecting to a
database and flags operations that commonly take a long or exclusive lock on a
large PostgreSQL table:

- ``create_index`` without ``postgresql_concurrently=True``
- adding a ``NOT NULL`` column with a server default
- ``alter_column`` that changes the type
- adding a unique/foreign-key constraint that validates existing rows
- raw ``CREATE INDEX`` SQL without ``CONCURRENTLY``
- duplicate revision IDs or multiple heads in the migration graph

The check is advisory: it sees the migration source, not table sizes, locks
currently held, or the database's pending revision. A clean result is not a
guarantee. Findings exit ``1``; ``--advisory`` reports them and exits ``0`` so a
team can adopt the check before making it blocking. ``--json`` emits one JSON
document for CI. Input/parse errors exit ``2``.

Usage::

    python scripts/migration_preflight.py
    python scripts/migration_preflight.py --json
    python scripts/migration_preflight.py --versions path/to/versions --advisory
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_VERSIONS = ROOT / "migrations" / "versions"

_RAW_INDEX_RE = re.compile(r"\bcreate\s+(?:unique\s+)?index\b(?!\s+concurrently)", re.IGNORECASE)


class PreflightError(Exception):
    """A migration directory could not be read or parsed."""


@dataclass
class Migration:
    path: Path
    revision: str | None = None
    down_revision: str | list[str] | None = None
    findings: list[dict[str, Any]] = field(default_factory=list)


def _constant_bool(node: ast.AST | None) -> bool | None:
    if isinstance(node, ast.Constant) and isinstance(node.value, bool):
        return node.value
    return None


def _call_name(node: ast.AST) -> str | None:
    if isinstance(node, ast.Call):
        if isinstance(node.func, ast.Attribute):
            return node.func.attr
        if isinstance(node.func, ast.Name):
            return node.func.id
    return None


def _call_keywords(node: ast.Call) -> dict[str, ast.AST]:
    return {keyword.arg: keyword.value for keyword in node.keywords if keyword.arg}


def _finding(
    path: Path,
    node: ast.AST,
    code: str,
    operation: str,
    message: str,
    suggestion: str,
) -> dict[str, Any]:
    return {
        "code": code,
        "file": path.name,
        "line": getattr(node, "lineno", 0),
        "operation": operation,
        "message": message,
        "suggestion": suggestion,
    }


def _is_column_call(node: ast.AST) -> bool:
    return _call_name(node) in {"Column", "column"}


def _alembic_version(tree: ast.Module) -> tuple[str | None, str | list[str] | None]:
    revision = None
    down_revision = None
    for statement in tree.body:
        if not isinstance(statement, ast.Assign) or len(statement.targets) != 1:
            continue
        target = statement.targets[0]
        if not isinstance(target, ast.Name):
            continue
        if target.id not in {"revision", "down_revision"}:
            continue
        try:
            value = ast.literal_eval(statement.value)
        except (ValueError, SyntaxError):
            continue
        if target.id == "revision" and isinstance(value, str):
            revision = value
        elif target.id == "down_revision" and (
            value is None or isinstance(value, (str, list, tuple))
        ):
            down_revision = list(value) if isinstance(value, (list, tuple)) else value
    return revision, down_revision


def _scan_upgrade(tree: ast.Module, path: Path) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    for node in ast.walk(tree):
        name = _call_name(node)
        if not isinstance(node, ast.Call) or name is None:
            continue
        keywords = _call_keywords(node)

        if name == "create_index":
            if _constant_bool(keywords.get("postgresql_concurrently")) is not True:
                findings.append(
                    _finding(
                        path,
                        node,
                        "index_not_concurrent",
                        "create_index",
                        "Index is built while holding a write lock on the table.",
                        "Use op.create_index(..., postgresql_concurrently=True) and run "
                        "the migration outside a transaction block.",
                    )
                )

        elif name == "add_column":
            columns = [arg for arg in node.args if _is_column_call(arg)]
            for column in columns:
                column_keywords = _call_keywords(column)
                nullable = _constant_bool(column_keywords.get("nullable"))
                has_default = "server_default" in column_keywords
                if nullable is False and has_default:
                    findings.append(
                        _finding(
                            path,
                            node,
                            "not_null_column_with_default",
                            "add_column",
                            "ADD COLUMN NOT NULL with a default takes an ACCESS EXCLUSIVE "
                            "lock and rewrites the table on PostgreSQL before 11.",
                            "Add the column as nullable, backfill in batches, then add the "
                            "constraint in a later migration.",
                        )
                    )

        elif name == "alter_column":
            if "type_" in keywords and not isinstance(keywords.get("type_"), ast.Constant):
                findings.append(
                    _finding(
                        path,
                        node,
                        "type_rewrite",
                        "alter_column",
                        "Changing a column type can rewrite the table and block writes.",
                        "Add a new column, backfill, switch reads/writes, then drop the old "
                        "column in a later migration.",
                    )
                )

        elif name in {"create_unique_constraint", "create_foreign_key"}:
            findings.append(
                _finding(
                    path,
                    node,
                    "constraint_validation",
                    name,
                    "Adding this constraint validates every existing row under a lock.",
                    "Add the constraint as NOT VALID, then VALIDATE CONSTRAINT in a later "
                    "migration.",
                )
            )

        elif name == "execute":
            for arg in node.args:
                if (
                    isinstance(arg, ast.Constant)
                    and isinstance(arg.value, str)
                    and _RAW_INDEX_RE.search(arg.value)
                ):
                    findings.append(
                        _finding(
                            path,
                            node,
                            "raw_index_not_concurrent",
                            "execute",
                            "Raw CREATE INDEX SQL does not use CONCURRENTLY.",
                            "Use CREATE INDEX CONCURRENTLY and run it outside a "
                            "transaction block, or op.create_index(..., "
                            "postgresql_concurrently=True).",
                        )
                    )
    return findings


def load_migrations(versions: Path) -> list[Migration]:
    """Parse every ``*.py`` migration in ``versions``."""
    if not versions.is_dir():
        raise PreflightError(f"migration directory not found: {versions}")
    files = sorted(versions.glob("*.py"))
    if not files:
        raise PreflightError(f"no migration files found in {versions}")

    migrations: list[Migration] = []
    for path in files:
        try:
            # utf-8-sig: some committed migrations start with a BOM, which the
            # Python import machinery accepts but ast.parse rejects.
            source = path.read_text(encoding="utf-8-sig")
            tree = ast.parse(source, filename=str(path))
        except (OSError, SyntaxError, UnicodeDecodeError) as exc:
            raise PreflightError(f"cannot parse {path.name}: {exc}") from exc
        revision, down_revision = _alembic_version(tree)
        migration = Migration(path=path, revision=revision, down_revision=down_revision)
        migration.findings.extend(_scan_upgrade(tree, path))
        migrations.append(migration)
    return migrations


def _ancestors(migrations: list[Migration], revision: str) -> set[str]:
    by_revision = {migration.revision: migration for migration in migrations if migration.revision}
    if revision not in by_revision:
        raise PreflightError(f"unknown revision for --since: {revision}")
    seen: set[str] = set()
    stack = [revision]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        migration = by_revision.get(current)
        if migration is None:
            continue
        down = migration.down_revision
        if isinstance(down, str):
            stack.append(down)
        elif isinstance(down, list):
            stack.extend(down)
    return seen


def graph_findings(migrations: list[Migration]) -> list[dict[str, Any]]:
    """Report duplicate revision IDs and multiple graph heads."""
    findings: list[dict[str, Any]] = []
    seen: dict[str, Path] = {}
    for migration in migrations:
        if not migration.revision:
            continue
        if migration.revision in seen:
            findings.append(
                {
                    "code": "duplicate_revision",
                    "file": migration.path.name,
                    "line": 0,
                    "operation": "revision",
                    "message": f"Revision {migration.revision} is also defined in "
                    f"{seen[migration.revision].name}.",
                    "suggestion": "Give each migration a unique revision id; then merge or "
                    "linearise the graph.",
                }
            )
        else:
            seen[migration.revision] = migration.path

    referenced: set[str] = set()
    for migration in migrations:
        down = migration.down_revision
        if isinstance(down, str):
            referenced.add(down)
        elif isinstance(down, list):
            referenced.update(down)
    heads = sorted(revision for revision in seen if revision not in referenced)
    if len(heads) > 1:
        findings.append(
            {
                "code": "multiple_heads",
                "file": "",
                "line": 0,
                "operation": "graph",
                "message": f"Multiple migration heads: {', '.join(heads)}.",
                "suggestion": "Add a merge migration or rebase the branch so the graph has one "
                "head before deploying.",
            }
        )
    return findings


def inspect(versions: Path, since: str | None = None) -> dict[str, Any]:
    """Return the structured preflight result for ``versions``.

    ``since`` limits operation findings to revisions that come after the given
    applied revision; graph checks always cover the whole directory.
    """
    migrations = load_migrations(versions)
    applied: set[str] = set()
    if since:
        applied = _ancestors(migrations, since)
    pending = [migration for migration in migrations if migration.revision not in applied]
    findings = [finding for migration in pending for finding in migration.findings]
    findings.extend(graph_findings(migrations))
    return {
        "ok": not findings,
        "advisory": False,
        "versions": str(versions),
        "since": since,
        "migration_count": len(pending),
        "findings": findings,
        "limitations": [
            "Does not connect to a database: table size, lock waits, and pending revisions "
            "are unknown.",
            "Only detects direct Alembic/SQL calls; dynamic SQL and data migrations are not "
            "analysed.",
            "A clean result does not prove a migration is fast or lock-free.",
        ],
    }


def _print_human(result: dict[str, Any], advisory: bool) -> None:
    print(f"migrations inspected: {result['migration_count']}")
    if not result["findings"]:
        print("preflight: no long-lock patterns detected")
        return
    print(f"preflight: {len(result['findings'])} finding(s)")
    for finding in result["findings"]:
        location = finding["file"] + (f":{finding['line']}" if finding["line"] else "")
        print(f"  [{finding['code']}] {location} {finding['message']}")
        print(f"    -> {finding['suggestion']}")
    if advisory:
        print("advisory mode: findings do not fail this run")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--versions",
        type=Path,
        default=DEFAULT_VERSIONS,
        help="Directory of Alembic migration files.",
    )
    parser.add_argument(
        "--since",
        help="Only check migrations after this applied revision (graph still "
        "covers every file). Use the revision from `flask db current`.",
    )
    parser.add_argument("--json", action="store_true", help="Emit one JSON document.")
    parser.add_argument(
        "--advisory",
        action="store_true",
        help="Report findings but exit 0 (use while adopting the check).",
    )
    args = parser.parse_args(argv)

    try:
        result = inspect(args.versions, since=args.since)
    except PreflightError as exc:
        error = {"ok": False, "error": {"code": "preflight_error", "message": str(exc)}}
        print(json.dumps(error), file=sys.stderr)
        return 2

    result["advisory"] = args.advisory
    if args.json:
        print(json.dumps(result, indent=2))
    else:
        _print_human(result, args.advisory)

    if result["findings"] and not args.advisory:
        return 1
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
