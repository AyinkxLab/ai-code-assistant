"""Organization-defined roles and their capabilities (#597).

A role is a named, reusable set of plain capabilities from
:data:`app.services.permissions.CAPABILITIES`. Roles belong to an organization
owner (``users.id``); there is no organizations table. Built-in ``owner``,
``admin`` and ``member`` roles are seeded both by the Alembic backfill and by
:func:`ensure_builtin_roles`, which is used by ``db.create_all()``-based tests
and fresh developer databases.

All public failures use :mod:`app.api.error_catalog` codes through
:class:`OrganizationRoleError`, so callers can render the same structured shape
as the rest of the admin console.
"""

from __future__ import annotations

import re

from app.api.error_catalog import CONFLICT, INVALID_INPUT, NOT_FOUND
from app.extensions import db
from app.models import OrganizationRole, OrganizationRoleCapability
from app.models.organization_role import (
    BUILTIN_ROLE_NAMES,
    MAX_DESCRIPTION_LENGTH,
    MAX_ROLE_NAME_LENGTH,
    ROLE_ADMIN,
    ROLE_MEMBER,
    ROLE_OWNER,
)
from app.services import permissions

_NAME_RE = re.compile(r"^[a-z][a-z0-9_-]*$")

#: Built-in capability sets are derived from the same plain action namespace as
#: the workspace permission matrix. An organization admin cannot transfer
#: ownership and the owner does not "leave" the workspace.
BUILTIN_CAPABILITIES: dict[str, tuple[str, ...]] = {
    ROLE_OWNER: tuple(cap for cap in permissions.CAPABILITIES if cap != "leave_workspace"),
    ROLE_ADMIN: (
        "view_members",
        "comment",
        "view_activity",
        "heartbeat",
        "manage_members",
        "manage_invitations",
        "manage_settings",
        "view_audit",
    ),
    ROLE_MEMBER: ("view_members", "comment", "view_activity", "heartbeat", "leave_workspace"),
}


class OrganizationRoleError(ValueError):
    """An invalid or forbidden custom-role operation with a catalog code."""

    def __init__(self, message: str, code: str = INVALID_INPUT) -> None:
        super().__init__(message)
        self.code = code


def normalize_name(value) -> str:
    """Validate and canonicalize a role name."""
    name = str(value or "").strip().lower()
    if not name or len(name) > MAX_ROLE_NAME_LENGTH or not _NAME_RE.match(name):
        raise OrganizationRoleError(
            "A role name must start with a letter and use 1-64 lowercase letters, "
            "digits, '-' or '_'."
        )
    return name


def normalize_capabilities(value) -> list[str]:
    """Validate a capability iterable (or comma-separated string)."""
    if isinstance(value, str):
        requested = [part.strip().lower() for part in value.split(",") if part.strip()]
    elif value is None:
        requested = []
    else:
        try:
            requested = [str(part).strip().lower() for part in value if str(part).strip()]
        except TypeError as exc:
            raise OrganizationRoleError("Capabilities must be a list of action names.") from exc
    if not requested:
        raise OrganizationRoleError("At least one capability is required.")
    unknown = sorted(set(requested) - set(permissions.CAPABILITIES))
    if unknown:
        raise OrganizationRoleError(f"Unknown capability: {unknown[0]}.")
    return sorted(set(requested))


def normalize_description(value) -> str | None:
    """Return a bounded optional description."""
    if value is None:
        return None
    description = str(value).strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise OrganizationRoleError("A description must be at most 300 characters.")
    return description or None


def ensure_builtin_roles(owner_id: int) -> list[OrganizationRole]:
    """Seed the three built-in roles for ``owner_id``; safe to call repeatedly.

    Existing built-in rows are kept as-is; only missing roles and missing
    capability grants are added. This makes the service compatible with tests
    that create the schema using ``db.create_all()`` instead of migrations.
    """
    rows: list[OrganizationRole] = []
    changed = False
    for name in BUILTIN_ROLE_NAMES:
        role = OrganizationRole.query.filter_by(owner_id=owner_id, name=name).first()
        if role is None:
            role = OrganizationRole(
                owner_id=owner_id,
                name=name,
                description=f"Built-in {name} role.",
                is_builtin=True,
            )
            db.session.add(role)
            changed = True
        elif not role.is_builtin:
            # A built-in name is reserved; repair a row left by an older build.
            role.is_builtin = True
            changed = True
        existing = {grant.capability for grant in role.capabilities}
        for capability in BUILTIN_CAPABILITIES[name]:
            if capability not in existing:
                db.session.add(OrganizationRoleCapability(role=role, capability=capability))
                changed = True
        rows.append(role)
    if changed:
        db.session.commit()
    return rows


def list_roles(owner_id: int) -> list[OrganizationRole]:
    """Return all roles for an organization, built-ins first."""
    ensure_builtin_roles(owner_id)
    return (
        OrganizationRole.query.filter_by(owner_id=owner_id)
        .order_by(
            OrganizationRole.is_builtin.desc(),
            OrganizationRole.name.asc(),
            OrganizationRole.id.asc(),
        )
        .all()
    )


def get_role(owner_id: int, role_id: int) -> OrganizationRole:
    """Return one owned role or raise a ``not_found`` error."""
    ensure_builtin_roles(owner_id)
    role = OrganizationRole.query.filter_by(id=role_id, owner_id=owner_id).first()
    if role is None:
        raise OrganizationRoleError("Role not found.", code=NOT_FOUND)
    return role


def create_role(
    owner_id: int,
    name,
    capabilities,
    *,
    description=None,
    created_by_id: int | None = None,
) -> OrganizationRole:
    """Create a custom role owned by ``owner_id`` and return it."""
    ensure_builtin_roles(owner_id)
    clean_name = normalize_name(name)
    if clean_name in BUILTIN_ROLE_NAMES:
        raise OrganizationRoleError("Built-in role names are reserved.", code=CONFLICT)
    if OrganizationRole.query.filter_by(owner_id=owner_id, name=clean_name).first() is not None:
        raise OrganizationRoleError("A role with that name already exists.", code=CONFLICT)

    role = OrganizationRole(
        owner_id=owner_id,
        name=clean_name,
        description=normalize_description(description),
        is_builtin=False,
    )
    for capability in normalize_capabilities(capabilities):
        role.capabilities.append(OrganizationRoleCapability(capability=capability))
    # ``created_by_id`` is carried in the route's audit entry; the role table
    # itself does not track creators (roles are owned, not personal).
    db.session.add(role)
    db.session.commit()
    return role


def update_role(
    owner_id: int,
    role_id: int,
    *,
    name=None,
    capabilities=None,
    description=...,
) -> OrganizationRole:
    """Rename, re-describe, or replace the capabilities of a custom role."""
    role = get_role(owner_id, role_id)
    if role.is_builtin:
        raise OrganizationRoleError("Built-in roles cannot be modified.", code=CONFLICT)
    if name is not None:
        clean_name = normalize_name(name)
        if clean_name in BUILTIN_ROLE_NAMES:
            raise OrganizationRoleError("Built-in role names are reserved.", code=CONFLICT)
        duplicate = OrganizationRole.query.filter_by(owner_id=owner_id, name=clean_name).first()
        if duplicate is not None and duplicate.id != role.id:
            raise OrganizationRoleError("A role with that name already exists.", code=CONFLICT)
        role.name = clean_name
    if capabilities is not None:
        clean_capabilities = normalize_capabilities(capabilities)
        role.capabilities.clear()
        for capability in clean_capabilities:
            role.capabilities.append(OrganizationRoleCapability(capability=capability))
    if description is not ...:
        role.description = normalize_description(description)
    db.session.commit()
    return role


def delete_role(owner_id: int, role_id: int) -> None:
    """Delete a custom role and all of its capability grants."""
    role = get_role(owner_id, role_id)
    if role.is_builtin:
        raise OrganizationRoleError("Built-in roles cannot be deleted.", code=CONFLICT)
    db.session.delete(role)
    db.session.commit()


def capabilities_for_role(owner_id: int, role_name: str) -> list[str]:
    """Return the capabilities of one owned role (empty for unknown roles)."""
    name = str(role_name or "").strip().lower()
    role = OrganizationRole.query.filter_by(owner_id=owner_id, name=name).first()
    if role is None:
        return []
    return role.capability_names


__all__ = [
    "BUILTIN_CAPABILITIES",
    "BUILTIN_ROLE_NAMES",
    "OrganizationRoleError",
    "capabilities_for_role",
    "create_role",
    "delete_role",
    "ensure_builtin_roles",
    "get_role",
    "list_roles",
    "normalize_capabilities",
    "normalize_description",
    "normalize_name",
    "update_role",
]
