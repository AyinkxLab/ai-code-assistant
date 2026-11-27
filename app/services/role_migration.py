"""Legacy role migration and default-role backfill (#605).

Older deployments used free-form role strings (``reader``, ``member``,
``administrator``, ...) in ``workspace_members``. This module migrates only
*known* aliases to the canonical capability-backed role names, leaves unknown
values untouched, and resolves duplicate memberships deterministically without
ever inventing an owner or admin grant.

Every applied change is stored in ``role_migration_records`` with exact before
and after snapshots, so ``--rollback`` restores the previous state instead of
guessing. Built-in role rows are backfilled per organization owner and are
marked ``is_builtin``; custom roles added later remain additive.

Usage::

    python scripts/role_migration.py --dry-run
    python scripts/role_migration.py --apply
    python scripts/role_migration.py --verify
    python scripts/role_migration.py --rollback

``--dry-run`` is the default. Exit codes: ``0`` success, ``1`` verification
failure, ``2`` input/database error (with a structured JSON error).
"""

from __future__ import annotations

import argparse
import json
import sys
from collections import defaultdict
from typing import Any

from app.extensions import db
from app.models import OrganizationRole, RoleMigrationRecord, Workspace, WorkspaceMember
from app.models.workspace_member import STATUS_ACTIVE, STATUS_REMOVED
from app.services.permissions import CAPABILITIES

#: Canonical role names understood by the workspace and organization layers.
CANONICAL_ROLES = ("viewer", "contributor", "admin", "owner")
ROLE_RANK = {role: rank for rank, role in enumerate(CANONICAL_ROLES)}

#: Legacy strings that can be translated without changing their meaning. An
#: unknown string is never guessed: it is reported and left alone.
LEGACY_ROLE_ALIASES = {
    "reader": "viewer",
    "guest": "viewer",
    "member": "contributor",
    "editor": "contributor",
    "administrator": "admin",
    "org_admin": "admin",
    "workspace_owner": "owner",
}

#: Default role definitions backfilled for each organization owner. The values
#: mirror the authoritative capability matrix in ``app.services.permissions``;
#: ``admin`` intentionally has no workspace capabilities.
BUILTIN_ROLE_NAMES = CANONICAL_ROLES
BUILTIN_ROLE_CAPABILITIES = {
    role: tuple(sorted(cap for cap, (roles, _) in CAPABILITIES.items() if role in roles))
    for role in BUILTIN_ROLE_NAMES
}

_ROLE_MIGRATION_ERROR = "role_migration_error"


class RoleMigrationError(Exception):
    """A structured, public error from the migration CLI or its helpers."""

    def __init__(self, code: str, message: str, details: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.details = details or {}

    def to_dict(self) -> dict[str, Any]:
        """Return the structured error payload."""
        return {
            "error": {
                "code": self.code,
                "message": self.message,
                "details": self.details,
            }
        }


def parse_owner_id(value: str | None) -> int | None:
    """Validate an optional ``--owner-id`` filter."""
    if value in (None, ""):
        return None
    try:
        owner_id = int(value)
    except (TypeError, ValueError) as exc:
        raise RoleMigrationError(
            "invalid_owner_id",
            "owner-id must be a positive integer.",
            {"owner_id": value},
        ) from exc
    if owner_id < 1:
        raise RoleMigrationError(
            "invalid_owner_id",
            "owner-id must be a positive integer.",
            {"owner_id": value},
        )
    return owner_id


def _snapshot(row: WorkspaceMember) -> dict[str, Any]:
    return {
        "id": row.id,
        "workspace_id": row.workspace_id,
        "user_id": row.user_id,
        "role": row.role,
        "status": row.status,
    }


def _canonical_role(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    role = value.strip().lower()
    if role in CANONICAL_ROLES:
        return role
    return LEGACY_ROLE_ALIASES.get(role)


def _workspaces(owner_id: int | None) -> list[Workspace]:
    query = Workspace.query
    if owner_id is not None:
        query = query.filter_by(user_id=owner_id)
    return query.order_by(Workspace.id).all()


def _default_roles_to_create(workspaces: list[Workspace]) -> list[dict[str, Any]]:
    owner_ids = sorted({workspace.user_id for workspace in workspaces if workspace.user_id})
    if not owner_ids:
        return []
    existing = {
        (role.owner_id, role.name)
        for role in OrganizationRole.query.filter(OrganizationRole.owner_id.in_(owner_ids)).all()
    }
    return [
        {
            "owner_id": owner_id,
            "name": name,
            "capabilities": list(BUILTIN_ROLE_CAPABILITIES[name]),
        }
        for owner_id in owner_ids
        for name in BUILTIN_ROLE_NAMES
        if (owner_id, name) not in existing
    ]


def plan_role_migration(*, owner_id: int | None = None) -> dict[str, Any]:
    """Return the exact changes that ``apply_role_migration`` would make."""
    workspaces = _workspaces(owner_id)
    workspace_ids = [workspace.id for workspace in workspaces]
    if workspace_ids:
        rows = (
            WorkspaceMember.query.filter(WorkspaceMember.workspace_id.in_(workspace_ids))
            .order_by(WorkspaceMember.workspace_id, WorkspaceMember.user_id, WorkspaceMember.id)
            .all()
        )
    else:
        rows = []

    grouped: dict[tuple[int, int], list[WorkspaceMember]] = defaultdict(list)
    for row in rows:
        grouped[(row.workspace_id, row.user_id)].append(row)

    changes: list[dict[str, Any]] = []
    duplicates: list[dict[str, Any]] = []
    unknown: list[dict[str, Any]] = []
    skipped: list[dict[str, Any]] = []

    for (workspace_id, user_id), group in sorted(grouped.items()):
        canonical_by_id = {}
        for row in group:
            canonical = _canonical_role(row.role)
            if canonical is None:
                unknown.append(
                    {
                        "workspace_id": workspace_id,
                        "user_id": user_id,
                        "row_id": row.id,
                        "role": row.role,
                        "reason": "unknown_legacy_role",
                    }
                )
                continue
            canonical_by_id[row.id] = canonical

        if not canonical_by_id:
            skipped.append(
                {
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "reason": "no known role to migrate",
                }
            )
            continue

        target = max(canonical_by_id.values(), key=lambda role: ROLE_RANK[role])
        if len(group) > 1:
            duplicates.append(
                {
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "roles": [row.role for row in group],
                    "keep_role": target,
                }
            )

        known_rows = [row for row in group if row.id in canonical_by_id]
        # Prefer an active, highest-ranked known row. Unknown rows are never
        # promoted, even when they are the only active row in the group.
        winner = min(
            known_rows,
            key=lambda row: (
                0 if row.status == STATUS_ACTIVE else 1,
                -ROLE_RANK[canonical_by_id[row.id]],
                row.id,
            ),
        )

        for row in group:
            before = _snapshot(row)
            after = dict(before)
            if row.id == winner.id:
                after["role"] = target
                after["status"] = STATUS_ACTIVE
                reason = "duplicate_resolution" if len(group) > 1 else "legacy_alias"
            elif row.status == STATUS_ACTIVE:
                after["status"] = STATUS_REMOVED
                reason = "duplicate_resolution"
            else:
                continue
            if after != before:
                changes.append(
                    {
                        "workspace_id": workspace_id,
                        "user_id": user_id,
                        "row_id": row.id,
                        "before": before,
                        "after": after,
                        "reason": reason,
                    }
                )

    return {
        "scanned_workspaces": len(workspaces),
        "scanned_memberships": len(rows),
        "role_changes": changes,
        "duplicates": duplicates,
        "unknown": unknown,
        "skipped": skipped,
        "default_roles_to_create": _default_roles_to_create(workspaces),
    }


def _record_changes(changes: list[dict[str, Any]], created_role_ids: dict[int, list[int]]) -> None:
    grouped: dict[tuple[int, int], list[dict[str, Any]]] = defaultdict(list)
    for change in changes:
        key = (change["workspace_id"], change["user_id"])
        grouped[key].append(
            {
                "row_id": change["row_id"],
                "before": change["before"],
                "after": change["after"],
            }
        )

    for (workspace_id, user_id), items in sorted(grouped.items()):
        record = RoleMigrationRecord(
            workspace_id=workspace_id,
            user_id=user_id,
            before_state=json.dumps([item["before"] for item in items], sort_keys=True),
            after_state=json.dumps([item["after"] for item in items], sort_keys=True),
            created_roles="[]",
        )
        db.session.add(record)

    for owner_id, role_ids in sorted(created_role_ids.items()):
        db.session.add(
            RoleMigrationRecord(
                workspace_id=None,
                user_id=owner_id,
                before_state="[]",
                after_state="[]",
                created_roles=json.dumps(role_ids, sort_keys=True),
            )
        )


def apply_role_migration(*, owner_id: int | None = None) -> dict[str, Any]:
    """Apply the planned changes and record an exact undo ledger."""
    result = plan_role_migration(owner_id=owner_id)
    created_roles = []
    created_role_ids: dict[int, list[int]] = defaultdict(list)

    for item in result["default_roles_to_create"]:
        role = OrganizationRole(
            owner_id=item["owner_id"],
            name=item["name"],
            description=f"Built-in {item['name']} role",
            is_builtin=True,
        )
        role.capabilities = item["capabilities"]
        db.session.add(role)
        created_roles.append(role)
    db.session.flush()

    for role in created_roles:
        created_role_ids[role.owner_id].append(role.id)

    for change in result["role_changes"]:
        row = db.session.get(WorkspaceMember, change["row_id"])
        if row is None:  # pragma: no cover - rows cannot vanish inside a run
            continue
        row.role = change["after"]["role"]
        row.status = change["after"]["status"]

    _record_changes(result["role_changes"], created_role_ids)
    db.session.commit()

    result["applied_changes"] = result["role_changes"]
    result["created_roles"] = [role.to_dict() for role in created_roles]
    return result


def rollback_role_migration() -> dict[str, Any]:
    """Restore every snapshot taken by prior migrations, newest first."""
    records = RoleMigrationRecord.query.order_by(RoleMigrationRecord.id.desc()).all()
    restored: list[dict[str, Any]] = []
    deleted_roles: list[int] = []

    for record in records:
        for snapshot in record.before:
            row_id = snapshot.get("id")
            row = db.session.get(WorkspaceMember, row_id) if isinstance(row_id, int) else None
            if row is None:
                continue
            row.role = snapshot["role"]
            row.status = snapshot["status"]
            restored.append(_snapshot(row))
        for role_id in record.created_role_ids:
            role = db.session.get(OrganizationRole, role_id)
            if role is not None and role.is_builtin:
                db.session.delete(role)
                deleted_roles.append(role_id)
        db.session.delete(record)

    db.session.commit()
    return {
        "records": len(records),
        "restored": restored,
        "deleted_roles": deleted_roles,
    }


def verify_role_migration() -> dict[str, Any]:
    """Report any legacy role, active duplicate, or missing built-in role."""
    problems: list[dict[str, Any]] = []
    workspaces = _workspaces(None)
    workspace_ids = [workspace.id for workspace in workspaces]
    rows = (
        WorkspaceMember.query.filter(WorkspaceMember.workspace_id.in_(workspace_ids)).all()
        if workspace_ids
        else []
    )
    active_groups: dict[tuple[int, int], int] = defaultdict(int)

    for row in rows:
        if _canonical_role(row.role) is None:
            problems.append(
                {
                    "workspace_id": row.workspace_id,
                    "user_id": row.user_id,
                    "reason": f"unmigrated role {row.role!r}",
                }
            )
        if row.status == STATUS_ACTIVE:
            active_groups[(row.workspace_id, row.user_id)] += 1

    for (workspace_id, user_id), count in sorted(active_groups.items()):
        if count > 1:
            problems.append(
                {
                    "workspace_id": workspace_id,
                    "user_id": user_id,
                    "reason": f"{count} active memberships",
                }
            )

    for item in _default_roles_to_create(workspaces):
        problems.append(
            {
                "workspace_id": None,
                "user_id": item["owner_id"],
                "reason": f"missing built-in role {item['name']!r}",
            }
        )

    return {
        "scanned_workspaces": len(workspaces),
        "ok": not problems,
        "problems": problems,
    }


def format_plan(result: dict[str, Any]) -> str:
    """Render a stable, human-readable dry-run report."""
    lines = [
        "Role migration (dry run)",
        f"Workspaces scanned: {result['scanned_workspaces']}",
        f"Memberships scanned: {result['scanned_memberships']}",
        f"Role/status changes to apply: {len(result['role_changes'])}",
        f"Duplicate legacy groups: {len(result['duplicates'])}",
        f"Unknown legacy roles skipped: {len(result['unknown'])}",
        f"Default built-in roles to backfill: {len(result['default_roles_to_create'])}",
    ]
    for change in result["role_changes"]:
        before = change["before"]
        after = change["after"]
        lines.append(
            f"  would change: workspace={change['workspace_id']} user={change['user_id']} "
            f"row={change['row_id']} {before['role']}/{before['status']} -> "
            f"{after['role']}/{after['status']} ({change['reason']})"
        )
    for row in result["unknown"]:
        lines.append(
            f"  skipped unknown: workspace={row['workspace_id']} user={row['user_id']} "
            f"role={row['role']!r}"
        )
    for row in result["duplicates"]:
        lines.append(
            f"  duplicate: workspace={row['workspace_id']} user={row['user_id']} "
            f"roles={row['roles']} keep={row['keep_role']}"
        )
    for item in result["default_roles_to_create"]:
        lines.append(f"  would add built-in role: owner={item['owner_id']} role={item['name']}")
    lines.append("No changes written. Re-run with --apply to write.")
    return "\n".join(lines)


def format_verify(result: dict[str, Any]) -> str:
    """Render verification output."""
    if result["ok"]:
        return f"Role migration verification passed for {result['scanned_workspaces']} workspaces."
    lines = [f"Role migration verification failed ({len(result['problems'])} problem(s)):"]
    for problem in result["problems"]:
        lines.append(
            f"  workspace={problem['workspace_id']} user={problem['user_id']}: "
            f"{problem['reason']}"
        )
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


def main(argv: list[str] | None = None, *, app=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--dry-run", action="store_true", help="Report the plan (default).")
    mode.add_argument("--apply", action="store_true", help="Write the migration.")
    mode.add_argument("--rollback", action="store_true", help="Restore recorded snapshots.")
    mode.add_argument("--verify", action="store_true", help="Check for unmigrated rows.")
    parser.add_argument("--owner-id", help="Restrict the migration to one organization owner.")
    parser.add_argument("--json", action="store_true", help="Emit one JSON document.")
    args = parser.parse_args(argv)

    try:
        owner_id = parse_owner_id(args.owner_id)
        application = app if app is not None else _make_app()
        with application.app_context():
            if args.apply:
                result = apply_role_migration(owner_id=owner_id)
                text = (
                    f"Applied {len(result['applied_changes'])} change(s) and backfilled "
                    f"{len(result['created_roles'])} built-in role(s)."
                )
                _emit(result, as_json=args.json, text=text)
                return 0
            if args.rollback:
                result = rollback_role_migration()
                text = (
                    f"Restored {len(result['restored'])} membership(s) and removed "
                    f"{len(result['deleted_roles'])} backfilled role(s)."
                )
                _emit(result, as_json=args.json, text=text)
                return 0
            if args.verify:
                result = verify_role_migration()
                _emit(result, as_json=args.json, text=format_verify(result))
                return 0 if result["ok"] else 1
            result = plan_role_migration(owner_id=owner_id)
            _emit(result, as_json=args.json, text=format_plan(result))
            return 0
    except RoleMigrationError as exc:
        print(json.dumps(exc.to_dict(), sort_keys=True), file=sys.stderr)
        return 2
    except Exception as exc:  # pragma: no cover - CLI boundary
        payload = RoleMigrationError(_ROLE_MIGRATION_ERROR, "Role migration failed.").to_dict()
        payload["error"]["details"] = {"reason": type(exc).__name__}
        print(json.dumps(payload, sort_keys=True), file=sys.stderr)
        return 2


if __name__ == "__main__":  # pragma: no cover - CLI entry point
    raise SystemExit(main())
