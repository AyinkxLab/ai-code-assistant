"""Custom organization role CRUD (#598).

Roles are scoped to the organization owner (``Workspace.user_id``) and can only
be managed by an organization owner or admin. Built-in rows backfilled by
``scripts/role_migration.py`` are immutable: they can be listed and read, but
never renamed, edited, or deleted. Custom roles are additive and can grant only
capability names already present in the authoritative workspace capability map;
they never change ``role_can`` or ``capabilities_for_role`` for a built-in role.

Every mutation appends an audit row with the acting user and exact before/after
snapshots. No token material or other secret is stored in role data.
"""

from __future__ import annotations

import re
from typing import Any

from app.extensions import db
from app.models import OrganizationRole, Workspace
from app.models.organization_role import MAX_DESCRIPTION_LENGTH, MAX_ROLE_NAME_LENGTH
from app.services import audit
from app.services.organizations import is_organization_admin
from app.services.permissions import CAPABILITIES

VALID_CAPABILITIES = frozenset(CAPABILITIES)
_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9 _-]*$")

FORBIDDEN = "forbidden"
NOT_FOUND = "not_found"
INVALID_INPUT = "invalid_input"
ROLE_NAME_EXISTS = "role_name_exists"
BUILTIN_ROLE_PROTECTED = "builtin_role_protected"

_ALLOWED_FIELDS = frozenset({"name", "description", "capabilities"})


class OrganizationRoleError(Exception):
    """A structured error from role validation or authorization."""

    def __init__(
        self,
        code: str,
        status: int,
        message: str,
        details: dict[str, Any] | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.status = status
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


def validate_name(value: Any) -> str:
    """Return a normalized custom role name or raise a structured error."""
    if not isinstance(value, str):
        raise OrganizationRoleError(
            INVALID_INPUT, 400, "Role name must be a string.", {"field": "name"}
        )
    name = value.strip()
    if not name or len(name) > MAX_ROLE_NAME_LENGTH or not _NAME_PATTERN.fullmatch(name):
        raise OrganizationRoleError(
            INVALID_INPUT,
            400,
            "Role name must be 1-"
            f"{MAX_ROLE_NAME_LENGTH} characters: letters, digits, spaces, '_' or '-'.",
            {"field": "name"},
        )
    return name


def validate_description(value: Any) -> str | None:
    """Return a trimmed description or ``None``."""
    if value is None:
        return None
    if not isinstance(value, str):
        raise OrganizationRoleError(
            INVALID_INPUT, 400, "Role description must be a string.", {"field": "description"}
        )
    description = value.strip()
    if len(description) > MAX_DESCRIPTION_LENGTH:
        raise OrganizationRoleError(
            INVALID_INPUT,
            400,
            f"Role description must be at most {MAX_DESCRIPTION_LENGTH} characters.",
            {"field": "description"},
        )
    return description or None


def validate_capabilities(value: Any) -> list[str]:
    """Return sorted, deduplicated capability names or raise a structured error."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise OrganizationRoleError(
            INVALID_INPUT,
            400,
            "Capabilities must be a list of known capability names.",
            {"field": "capabilities"},
        )
    for item in value:
        if not isinstance(item, str) or item not in VALID_CAPABILITIES:
            raise OrganizationRoleError(
                INVALID_INPUT,
                400,
                "Unknown capability name.",
                {"field": "capabilities", "capability": item if isinstance(item, str) else None},
            )
    return sorted(set(value))


def _require_admin(workspace_id: int, actor) -> Workspace:
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None or workspace.user_id is None:
        raise OrganizationRoleError(NOT_FOUND, 404, "Organization not found.")
    if actor is not None and not is_organization_admin(workspace_id, actor):
        raise OrganizationRoleError(FORBIDDEN, 403, "Organization admin access required.")
    return workspace


def _validate_fields(data: Any, *, partial: bool) -> dict[str, Any]:
    if not isinstance(data, dict) or not set(data) <= _ALLOWED_FIELDS:
        unknown = sorted(set(data) - _ALLOWED_FIELDS) if isinstance(data, dict) else []
        raise OrganizationRoleError(
            INVALID_INPUT,
            400,
            "A JSON object with supported role fields is required.",
            {"unknown_fields": unknown},
        )
    if partial and not data:
        raise OrganizationRoleError(INVALID_INPUT, 400, "At least one role field must be provided.")
    if not partial and "name" not in data:
        raise OrganizationRoleError(INVALID_INPUT, 400, "Role name is required.", {"field": "name"})

    result: dict[str, Any] = {}
    if "name" in data:
        result["name"] = validate_name(data["name"])
    if "description" in data:
        result["description"] = validate_description(data["description"])
    if "capabilities" in data:
        result["capabilities"] = validate_capabilities(data["capabilities"])
    return result


def _role(owner_id: int, role_id: int) -> OrganizationRole:
    role = OrganizationRole.query.filter_by(id=role_id, owner_id=owner_id).first()
    if role is None:
        raise OrganizationRoleError(NOT_FOUND, 404, "Role not found.")
    return role


def _assert_available_name(owner_id: int, name: str, *, exclude_id: int | None = None) -> None:
    query = OrganizationRole.query.filter_by(owner_id=owner_id, name=name)
    if exclude_id is not None:
        query = query.filter(OrganizationRole.id != exclude_id)
    if query.first() is not None:
        raise OrganizationRoleError(
            ROLE_NAME_EXISTS, 409, "A role with that name already exists.", {"name": name}
        )


def _audit(
    action: str, workspace_id: int, actor, role_id: int | None, owner_id: int, *, before, after
) -> None:
    audit.record(
        action,
        workspace_id=workspace_id,
        user=actor,
        target_type="organization_role",
        target_id=role_id,
        metadata={
            "actor_id": getattr(actor, "id", None),
            "owner_id": owner_id,
            "before": before,
            "after": after,
        },
    )


def list_roles(workspace_id: int, *, actor=None) -> list[dict]:
    """Return all built-in and custom roles for the caller's organization."""
    workspace = _require_admin(workspace_id, actor)
    roles = (
        OrganizationRole.query.filter_by(owner_id=workspace.user_id)
        .order_by(OrganizationRole.is_builtin.desc(), OrganizationRole.name)
        .all()
    )
    return [role.to_dict() for role in roles]


def get_role(workspace_id: int, role_id: int, *, actor=None) -> dict:
    """Return one organization role."""
    workspace = _require_admin(workspace_id, actor)
    return _role(workspace.user_id, role_id).to_dict()


def create_role(workspace_id: int, data: Any, *, actor=None, commit: bool = True) -> dict:
    """Create a custom role and audit the actor and resulting state."""
    workspace = _require_admin(workspace_id, actor)
    fields = _validate_fields(data, partial=False)
    _assert_available_name(workspace.user_id, fields["name"])

    role = OrganizationRole(
        owner_id=workspace.user_id,
        name=fields["name"],
        description=fields.get("description"),
        is_builtin=False,
        created_by_id=getattr(actor, "id", None),
        updated_by_id=getattr(actor, "id", None),
    )
    role.capabilities = fields.get("capabilities", [])
    db.session.add(role)
    db.session.flush()
    after = role.to_dict()
    _audit(
        audit.ORG_ROLE_CREATED,
        workspace_id,
        actor,
        role.id,
        workspace.user_id,
        before=None,
        after=after,
    )
    if commit:
        db.session.commit()
    return after


def update_role(
    workspace_id: int, role_id: int, data: Any, *, actor=None, commit: bool = True
) -> dict:
    """Update a custom role and audit the exact before/after values."""
    workspace = _require_admin(workspace_id, actor)
    role = _role(workspace.user_id, role_id)
    if role.is_builtin:
        raise OrganizationRoleError(BUILTIN_ROLE_PROTECTED, 409, "Built-in roles cannot be edited.")
    fields = _validate_fields(data, partial=True)
    if "name" in fields:
        _assert_available_name(workspace.user_id, fields["name"], exclude_id=role.id)

    before = role.to_dict()
    for field in ("name", "description"):
        if field in fields:
            setattr(role, field, fields[field])
    if "capabilities" in fields:
        role.capabilities = fields["capabilities"]
    role.updated_by_id = getattr(actor, "id", None)
    db.session.flush()
    after = role.to_dict()
    _audit(
        audit.ORG_ROLE_UPDATED,
        workspace_id,
        actor,
        role.id,
        workspace.user_id,
        before=before,
        after=after,
    )
    if commit:
        db.session.commit()
    return after


def delete_role(workspace_id: int, role_id: int, *, actor=None, commit: bool = True) -> dict:
    """Delete a custom role and audit the removed state."""
    workspace = _require_admin(workspace_id, actor)
    role = _role(workspace.user_id, role_id)
    if role.is_builtin:
        raise OrganizationRoleError(
            BUILTIN_ROLE_PROTECTED, 409, "Built-in roles cannot be deleted."
        )
    before = role.to_dict()
    db.session.delete(role)
    _audit(
        audit.ORG_ROLE_DELETED,
        workspace_id,
        actor,
        role_id,
        workspace.user_id,
        before=before,
        after=None,
    )
    if commit:
        db.session.commit()
    return {"deleted": True, "id": role_id}


__all__ = [
    "BUILTIN_ROLE_PROTECTED",
    "FORBIDDEN",
    "INVALID_INPUT",
    "NOT_FOUND",
    "ROLE_NAME_EXISTS",
    "VALID_CAPABILITIES",
    "OrganizationRoleError",
    "create_role",
    "delete_role",
    "get_role",
    "list_roles",
    "update_role",
    "validate_capabilities",
]
