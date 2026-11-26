"""Operator CLI for approval-request maintenance (#601)."""

from __future__ import annotations

import click
from flask import current_app

from app.services import approvals


def register_approval_cli(app) -> None:
    """Register the ``flask approvals`` command group."""

    @app.cli.group("approvals")
    def approvals_cli():
        """Manage sensitive-action approval requests."""

    @approvals_cli.command("expire")
    @click.option("--dry-run", is_flag=True, help="Report stale rows without changing them.")
    def expire_command(dry_run: bool):
        """Mark pending approval requests past their deadline as expired."""
        with current_app.app_context():
            if dry_run:
                moment = approvals._moment(None)
                count = (
                    approvals.ApprovalRequest.query.filter_by(status="pending")
                    .filter(approvals.ApprovalRequest.expires_at <= approvals._naive(moment))
                    .count()
                )
                click.echo(f"{count} approval request(s) would expire.")
                return
            count = approvals.expire_stale()
            click.echo(f"Expired {count} approval request(s).")
