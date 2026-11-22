"""Operator CLI for audit integrity and retention (Phase 11, #606, #608).

Usage::

    flask audit verify [--workspace-id N] [--json]
    flask audit retention show --workspace-id N [--json]
    flask audit retention set --workspace-id N --days N
                              [--legal-hold/--no-legal-hold] [--json]
    flask audit purge --workspace-id N [--dry-run] [--json]

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


def _fail_from(exc: Exception, as_json: bool) -> None:
    """Map a service exception to a stable CLI error code."""
    if isinstance(exc, PermissionError):
        code = "forbidden"
    elif isinstance(exc, LookupError):
        code = "not_found"
    else:
        code = "invalid_input"
    _fail(str(exc), code, as_json)


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

    @audit_group.group("retention")
    def retention_group():
        """Read and update the workspace audit retention policy."""

    @retention_group.command("show")
    @click.option("--workspace-id", type=int, required=True, help="Workspace to inspect.")
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def retention_show(workspace_id: int, as_json: bool) -> int:
        """Show the effective retention policy (stored or defaults)."""
        from app.extensions import db
        from app.models import Workspace
        from app.services.audit_retention import policy_snapshot

        if workspace_id < 1:
            _fail("workspace-id must be a positive integer.", "invalid_input", as_json)
        if db.session.get(Workspace, workspace_id) is None:
            _fail("Workspace not found.", "not_found", as_json)

        policy = policy_snapshot(workspace_id)
        if as_json:
            _write_json(policy)
        else:
            click.echo(
                f"workspace={workspace_id} retention_days={policy['retention_days']} "
                f"legal_hold={policy['legal_hold']}"
            )
        return EXIT_OK

    @retention_group.command("set")
    @click.option("--workspace-id", type=int, required=True, help="Workspace to update.")
    @click.option("--days", type=int, required=True, help="Retention window in days.")
    @click.option(
        "--legal-hold/--no-legal-hold",
        default=None,
        help="Activate or release a legal hold (default: leave unchanged).",
    )
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def retention_set(
        workspace_id: int,
        days: int,
        legal_hold: bool | None,
        as_json: bool,
    ) -> int:
        """Create or update a validated retention policy."""
        from app.services.audit_retention import set_policy

        try:
            policy = set_policy(
                workspace_id,
                retention_days=days,
                legal_hold=legal_hold,
            )
        except (ValueError, LookupError, PermissionError) as exc:
            _fail_from(exc, as_json)

        if as_json:
            _write_json(policy)
        else:
            click.echo(
                f"workspace={workspace_id} retention_days={policy['retention_days']} "
                f"legal_hold={policy['legal_hold']}"
            )
        return EXIT_OK

    @audit_group.command("purge")
    @click.option("--workspace-id", type=int, required=True, help="Workspace to purge.")
    @click.option("--dry-run", is_flag=True, help="Report what would be deleted.")
    @click.option("--json", "as_json", is_flag=True, help="Emit machine-readable JSON.")
    def purge(workspace_id: int, dry_run: bool, as_json: bool) -> int:
        """Delete audit rows older than the policy window (never under legal hold)."""
        from app.services.audit_retention import purge_expired

        try:
            result = purge_expired(workspace_id, dry_run=dry_run)
        except (ValueError, LookupError, PermissionError) as exc:
            _fail_from(exc, as_json)

        if as_json:
            _write_json(result)
        else:
            action = "Dry run" if dry_run else "Purge"
            suffix = f" skipped={result['skipped_reason']}" if result["skipped_reason"] else ""
            click.echo(
                f"{action} complete: deleted={result['deleted']} "
                f"retention_days={result['retention_days']} "
                f"cutoff={result['cutoff']}{suffix}"
            )
        return EXIT_OK


__all__ = ["EXIT_ERROR", "EXIT_OK", "EXIT_TAMPERED", "register_audit_cli"]
