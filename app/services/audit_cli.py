"""Operator CLI for audit integrity (Phase 11, #606).

Usage::

    flask audit verify [--workspace-id N] [--json]

With no ``--workspace-id`` every distinct chain (including the ``NULL`` chain
used by global events) is verified. Exit status is ``0`` when every chain is
intact, ``1`` when tampering is found, and ``2`` for invalid input.
"""

from __future__ import annotations

import json

import click
from flask import Flask

EXIT_OK = 0
EXIT_TAMPERED = 1
EXIT_ERROR = 2


def _write_json(data) -> None:
    click.echo(json.dumps(data, indent=2, sort_keys=True))


def _fail(message: str, code: str, as_json: bool) -> None:
    if as_json:
        _write_json({"error": message, "code": code})
    else:
        click.echo(message, err=True)
    raise click.exceptions.Exit(EXIT_ERROR)


def register_audit_cli(app: Flask) -> None:
    """Register the ``audit`` CLI command group on ``app``."""

    @app.cli.group("audit")
    def audit_group():
        """Audit integrity and retention commands."""

    @audit_group.command("verify")
    @click.option(
        "--workspace-id",
        type=int,
        default=None,
        help="Verify one workspace chain (default: every chain).",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def verify(workspace_id: int | None, as_json: bool) -> int:
        """Recompute hash chains and report modified, missing, or unhashed rows."""
        from app.extensions import db
        from app.models import Workspace
        from app.services.audit import verify_all_chains, verify_chain

        if workspace_id is not None:
            if workspace_id < 1:
                _fail("workspace-id must be a positive integer.", "invalid_input", as_json)
            if db.session.get(Workspace, workspace_id) is None:
                _fail("Workspace not found.", "not_found", as_json)
            chains = [verify_chain(workspace_id)]
        else:
            chains = verify_all_chains()

        ok = all(chain["ok"] for chain in chains)
        if as_json:
            _write_json({"ok": ok, "chains": chains})
        else:
            for chain in chains:
                scope = chain["workspace_id"]
                scope_label = "global" if scope is None else f"workspace {scope}"
                state = "ok" if chain["ok"] else "TAMPERED"
                click.echo(
                    f"{scope_label}: {state} checked={chain['checked']} "
                    f"unhashed={chain['unhashed']}"
                )
                for error in chain["errors"]:
                    click.echo(f"  row {error['id']}: {error['problem']}")
            if not chains:
                click.echo("No audit rows to verify.")

        if not ok:
            raise click.exceptions.Exit(EXIT_TAMPERED)
        return EXIT_OK


__all__ = ["EXIT_ERROR", "EXIT_OK", "EXIT_TAMPERED", "register_audit_cli"]
