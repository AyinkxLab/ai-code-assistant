"""Read-only project detection Flask CLI commands (issue #191).

Usage:

    flask project detect-stellar <path> [--json]

Walks a directory on the local filesystem, applies the same skip rules the
import pipeline uses (:func:`app.services.importing.should_skip`), builds
in-memory file stubs, and reports the Stellar/Soroban detection confidence and
evidence from :func:`app.services.stellar_detection.detect_stellar_project`.

Everything is read-only: no files are written and the database is never
touched. Exit codes: ``0`` success, ``3`` invalid input (missing path / not a
directory).
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass

import click
from flask import Flask

from app.services.importing import looks_binary, should_skip
from app.services.stellar_detection import detect_stellar_project

EXIT_OK = 0
EXIT_VALIDATION = 3

#: Files larger than this are stubbed with ``content=None`` so a stray large
#: artefact never dominates the walk (mirrors the import pipeline's cap).
MAX_STUB_BYTES = 1_000_000

#: Upper bound on files inspected in a single walk, so pointing the command at
#: a huge tree cannot hang the process.
MAX_FILES = 5000


@dataclass
class FileStub:
    """Minimal ``.path`` / ``.content`` pair consumed by ``detect_stellar_project``."""

    path: str
    content: str | None


def _relative(root: str, path: str) -> str:
    """Return ``path`` relative to ``root`` with forward slashes."""
    return os.path.relpath(path, root).replace(os.sep, "/")


def _read_text(path: str) -> str | None:
    """Read ``path`` as UTF-8 text, or ``None`` when it is binary or oversized."""
    try:
        if os.path.getsize(path) > MAX_STUB_BYTES:
            return None
        with open(path, "rb") as handle:
            raw = handle.read()
    except OSError:
        return None
    if looks_binary(raw):
        return None
    return raw.decode("utf-8", errors="replace")


def walk_project_files(root: str) -> list[FileStub]:
    """Return detection stubs for every non-skipped file under ``root``.

    Paths are relative and forward-slashed, and both directories and files pass
    through the shared ``should_skip`` rules, so detection sees exactly the
    files an import would keep.
    """
    stubs: list[FileStub] = []
    for dirpath, dirnames, filenames in os.walk(root):
        # Prune skipped directories in place so os.walk never descends into them.
        dirnames[:] = sorted(
            name
            for name in dirnames
            if not should_skip(_relative(root, os.path.join(dirpath, name)))
        )
        for name in sorted(filenames):
            relative = _relative(root, os.path.join(dirpath, name))
            if should_skip(relative):
                continue
            stubs.append(
                FileStub(path=relative, content=_read_text(os.path.join(dirpath, name)))
            )
            if len(stubs) >= MAX_FILES:
                return stubs
    return stubs


def register_project_cli(app: Flask) -> None:
    """Register the ``project`` CLI command group on ``app``."""

    @app.cli.group("project")
    def project_group():
        """Read-only project tooling (detection on the local filesystem)."""

    @project_group.command("detect-stellar")
    @click.argument("path", type=click.Path())
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def project_detect_stellar(path: str, as_json: bool) -> None:
        """Report Stellar/Soroban detection for a local directory (read-only)."""
        target = os.path.abspath(path)
        if not os.path.isdir(target):
            message = f"Not a directory: {path}"
            if as_json:
                click.echo(
                    json.dumps(
                        {"error": message, "exit_code": EXIT_VALIDATION},
                        indent=2,
                        sort_keys=True,
                    )
                )
            else:
                click.echo(message, err=True)
            raise click.exceptions.Exit(EXIT_VALIDATION)

        stubs = walk_project_files(target)
        signals = detect_stellar_project(stubs)

        if as_json:
            payload = {"path": target, "files_scanned": len(stubs)}
            payload.update(signals.to_dict())
            click.echo(json.dumps(payload, indent=2, sort_keys=True))
            return

        click.echo(f"path: {target}")
        click.echo(f"files_scanned: {len(stubs)}")
        click.echo(f"confidence: {signals.confidence}")
        click.echo(f"is_stellar: {signals.is_stellar}")
        click.echo(f"is_soroban: {signals.is_soroban}")
        if signals.evidence:
            click.echo("evidence:")
            for item in signals.evidence[:20]:
                click.echo(f"  - {item}")
        else:
            click.echo("evidence: (none)")
