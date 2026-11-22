"""Backfill explicit organization admin memberships (#636).

This repository has no separate ``organizations`` table: a workspace is the
organization and its owner (``workspaces.user_id``) is the organization owner.
Older workspaces predate the Phase 11 admin console, which lists an explicit
``admin`` membership for organization administrators in addition to the owner.

The backfill is additive and idempotent: for every workspace whose owner has no
active ``owner``/``admin`` membership row, it inserts one ``admin`` row. No
workspace, project, member, or usage row is changed or deleted, so rollback only
has to remove the rows this command created.

Usage::

    python scripts/organization_migration.py --dry-run
    python scripts/organization_migration.py --apply
    python scripts/organization_migration.py --verify
    python scripts/organization_migration.py --rollback

``--dry-run`` is the default. The command exits ``0`` on success, ``1`` when
verification finds an organization without an admin, and ``2`` on input or
database errors.
"""

from __future__ import annotations

import argparse
import json
import sys
from typing import Any

from app.extensions import db
from app.models import User, Workspace, WorkspaceMember
from app.models.workspace_member import STATUS_ACTIVE
from app.services.organizations import ADMIN_MEMBER_ROLES, ORG_ROLE_ADMIN


def _owner_membership(workspace: Workspace) -> WorkspaceMember | None:
    return WorkspaceMember.query.filter_by(
        workspace_id=workspace.id,
        user_id=workspace.user_id,
        status=STATUS_ACTIVE,
    ).first()


def _is_ready(workspace: Workspace) -> bool:
    membership = _owner_membership(workspace)
    return membership is not None and membership.role in ADMIN_MEMBER_ROLES


def plan_backfill() -> dict[str, Any]:
    """Return the rows that ``apply_backfill`` would add, without writing."""
    workspaces = Workspace.query.order_by(Workspace.id).all()
    ready = 0
    pending: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []
    for workspace in workspaces:
        owner = db.session.get(User, workspace.user_id) if workspace.user_id else None
        if owner is None:
            skipped.append({"workspace_id": workspace.id, "reason": "workspace has no owner"})
            continue
        if _is_ready(workspace):
            ready += 1
            continue
        if _owner_membership(workspace) is not None:
            # A stale owner membership exists; leave it untouched and report it.
            skipped.append(
                {"workspace_id": workspace.id, "reason": "owner membership already exists"}
            )
            continue
        pending.append(
            {
                "workspace_id": workspace.id,
                "workspace": workspace.name,
                "owner_id": owner.id,
                "owner": owner.username,
                "role": ORG_ROLE_ADMIN,
            }
        )
    return {
        "scanned": len(workspaces),
        "ready": ready,
        "pending": pending,
        "skipped": skipped,
    }


def apply_backfill() -> dict[str, Any]:
    """Create the planned admin memberships and return a summary."""
    result = plan_backfill()
    created = []
    for row in result["pending"]:
        membership = WorkspaceMember(
            workspace_id=row["workspace_id"],
            user_id=row["owner_id"],
            role=ORG_ROLE_ADMIN,
            status=STATUS_ACTIVE,
        )
        db.session.add(membership)
        created.append(row)
    db.session.commit()
    result["created"] = created
    return result


def rollback_backfill() -> dict[str, Any]:
    """Remove only the ``admin`` rows that the backfill may have created."""
    removed = []
    rows = WorkspaceMember.query.filter_by(role=ORG_ROLE_ADMIN, status=STATUS_ACTIVE).all()
    for membership in rows:
        workspace = db.session.get(Workspace, membership.workspace_id)
        if workspace is None or workspace.user_id != membership.user_id:
            continue
        removed.append(
            {
                "workspace_id": membership.workspace_id,
                "owner_id": membership.user_id,
            }
        )
        db.session.delete(membership)
    db.session.commit()
    return {"removed": removed}


def verify_backfill() -> dict[str, Any]:
    """Return every workspace that still has no active organization admin."""
    workspaces = Workspace.query.order_by(Workspace.id).all()
    problems = []
    for workspace in workspaces:
        if workspace.user_id is None:
            problems.append({"workspace_id": workspace.id, "reason": "workspace has no owner"})
            continue
        if not _is_ready(workspace):
            problems.append(
                {"workspace_id": workspace.id, "reason": "owner has no active admin membership"}
            )
    return {"scanned": len(workspaces), "ok": not problems, "problems": problems}


def format_plan(result: dict[str, Any]) -> str:
    """Render a dry-run report with a stable, documented shape."""
    lines = [
        "Organization migration (dry run)",
        f"Workspaces scanned: {result['scanned']}",
        f"Already organization-ready: {result['ready']}",
        f"Organization admin memberships to create: {len(result['pending'])}",
    ]
    for row in result["pending"]:
        lines.append(
            f"  would add admin: workspace={row['workspace_id']} "
            f"({row['workspace']}) owner={row['owner']} ({row['owner_id']})"
        )
    for row in result["skipped"]:
        lines.append(f"  skipped workspace={row['workspace_id']}: {row['reason']}")
    lines.append("No changes written. Re-run with --apply to write.")
    return "\n".join(lines)


def format_verify(result: dict[str, Any]) -> str:
    if result["ok"]:
        return f"Organization verification passed for {result['scanned']} workspaces."
    lines = [f"Organization verification failed ({len(result['problems'])} problem(s)):"]
    for problem in result["problems"]:
        lines.append(f"  workspace={problem['workspace_id']}: {problem['reason']}")
    return "\n".join(lines)


def _make_app():
    import os

    from app import create_app

    return create_app(os.getenv("APP_ENV", "development"))


def _emit(payload: dict[str, Any], *, as_json: bool, text: str) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(text)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report the plan (default).")
    mode.add_argument("--apply", action="store_true", help="Write the admin memberships.")
    mode.add_argument("--rollback", action="store_true", help="Remove backfilled admin rows.")
    mode.add_argument("--verify", action="store_true", help="Check every workspace has an admin.")
    parser.add_argument("--json", action="store_true", help="Emit one JSON document.")
    args = parser.parse_args(argv)

    try:
        app = _make_app()
        with app.app_context():
            if args.apply:
                result = apply_backfill()
                text = (
                    f"Created {len(result['created'])} organization admin membership(s). "
                    f"Scanned {result['scanned']} workspace(s)."
                )
                _emit(result, as_json=args.json, text=text)
                return 0
            if args.rollback:
                result = rollback_backfill()
                text = f"Removed {len(result['removed'])} organization admin membership(s)."
                _emit(result, as_json=args.json, text=text)
                return 0
            if args.verify:
                result = verify_backfill()
                _emit(result, as_json=args.json, text=format_verify(result))
                return 0 if result["ok"] else 1
            result = plan_backfill()
            _emit(result, as_json=args.json, text=format_plan(result))
            return 0
    except Exception as exc:  # pragma: no cover - CLI boundary
        print(f"organization migration failed: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
