"""Minimal SCIM group synchronization and safe role mapping (#593).

A directory group can map only to the non-owner roles ``viewer`` and
``contributor``. Mappings are applied to users that already exist in the
database; this module never creates accounts, grants the built-in ``owner``
role, grants an organization ``admin`` role, or consults a global admin flag.
Memberships created by SCIM are tracked with ``provisioning_source`` so a group
change only revokes roles it granted itself; manual memberships are left alone.

SCIM requests authenticate with the existing hashed API-token mechanism (read
for list/get, write for create/update, admin for delete). No new secret or
token material is introduced or stored.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extensions import db
from app.models import (
    GroupRoleMapping,
    ScimGroup,
    ScimGroupMember,
    User,
    Workspace,
    WorkspaceMember,
)
from app.models.group_role_mapping import (
    MAPPABLE_ROLES,
    MAX_DISPLAY_NAME_LENGTH,
    MAX_EXTERNAL_ID_LENGTH,
    PROVISIONING_MANUAL,
    PROVISIONING_SCIM,
)
from app.models.workspace_member import (
    ROLE_OWNER,
    STATUS_ACTIVE,
    STATUS_REMOVED,
)
from app.services import audit
from app.services.organizations import organization_role
from app.services.permissions import ROLE_RANK

SCIM_GROUP_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:Group"
MAX_MEMBERS = 1000

PROTECTED_ROLES = frozenset({ROLE_OWNER, "admin"})


class ScimError(ValueError):
    """Invalid SCIM or mapping input."""


class ScimPermissionDenied(PermissionError):
    """The caller is not an organization administrator."""


class ScimNotFound(LookupError):
    """The workspace, group, or mapping does not exist."""


class ScimConflict(Exception):
    """The operation conflicts with existing state."""


def _now() -> datetime:
    return datetime.now(UTC)


def validate_external_id(value) -> str:
    """Return a bounded directory group id."""
    if not isinstance(value, str) or not value.strip():
        raise ScimError("externalId is required.")
    normalized = value.strip()
    if len(normalized) > MAX_EXTERNAL_ID_LENGTH:
        raise ScimError(f"externalId must be at most {MAX_EXTERNAL_ID_LENGTH} characters.")
    return normalized


def validate_display_name(value) -> str:
    """Return a bounded group display name."""
    if not isinstance(value, str) or not value.strip():
        raise ScimError("displayName is required.")
    normalized = value.strip()
    if len(normalized) > MAX_DISPLAY_NAME_LENGTH:
        raise ScimError(f"displayName must be at most {MAX_DISPLAY_NAME_LENGTH} characters.")
    return normalized


def validate_role(value) -> str:
    """Return a safe mappable role; owner/admin are rejected."""
    if not isinstance(value, str) or value.strip() not in MAPPABLE_ROLES:
        raise ScimError("role must be viewer or contributor.")
    return value.strip()


def _workspace(workspace_id) -> Workspace:
    if isinstance(workspace_id, bool) or not isinstance(workspace_id, int) or workspace_id < 1:
        raise ScimError("workspace_id must be a positive integer.")
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None:
        raise ScimNotFound("Workspace not found.")
    return workspace


def _require_admin(workspace_id: int, actor) -> None:
    if actor is None or not getattr(actor, "is_authenticated", False):
        raise ScimPermissionDenied("Authentication is required.")
    if organization_role(workspace_id, actor) is None:
        # A global ``is_admin`` flag is deliberately ignored.
        raise ScimPermissionDenied("Organization admin access is required.")


def _get_mapping(workspace_id: int, mapping_id) -> GroupRoleMapping:
    if isinstance(mapping_id, bool) or not isinstance(mapping_id, int) or mapping_id < 1:
        raise ScimNotFound("Group role mapping not found.")
    row = db.session.get(GroupRoleMapping, mapping_id)
    if row is None or row.workspace_id != workspace_id:
        raise ScimNotFound("Group role mapping not found.")
    return row


def _get_group(group_id) -> ScimGroup:
    if isinstance(group_id, bool) or not isinstance(group_id, int) or group_id < 1:
        raise ScimNotFound("Group not found.")
    row = db.session.get(ScimGroup, group_id)
    if row is None:
        raise ScimNotFound("Group not found.")
    return row


def _payload_object(payload) -> dict:
    if not isinstance(payload, dict):
        raise ScimError("A JSON object body is required.")
    return payload


def _resolve_user_ids(workspace_id: int, members) -> list[int]:
    if members is None:
        return []
    if not isinstance(members, list):
        raise ScimError("members must be a list.")
    if len(members) > MAX_MEMBERS:
        raise ScimError(f"members must contain at most {MAX_MEMBERS} entries.")

    resolved: list[int] = []
    for item in members:
        if isinstance(item, dict):
            raw = item.get("value", item.get("email", item.get("userName")))
        elif isinstance(item, (str, int)) and not isinstance(item, bool):
            raw = item
        else:
            raise ScimError("Every member must be a user id or email address.")

        user = None
        if isinstance(raw, int):
            user = db.session.get(User, raw)
        elif isinstance(raw, str) and raw.strip().isdigit():
            user = db.session.get(User, int(raw.strip()))
        elif isinstance(raw, str) and raw.strip():
            user = User.query.filter_by(email=raw.strip().lower()).first()
        else:
            raise ScimError("Every member must be a user id or email address.")

        if user is None or not user.is_active:
            raise ScimError("Every SCIM group member must be an active user.")
        if user.id not in resolved:
            resolved.append(user.id)
    return resolved


def _find_member(workspace_id: int, user_id: int) -> WorkspaceMember | None:
    return WorkspaceMember.query.filter_by(workspace_id=workspace_id, user_id=user_id).first()


def _still_mapped(
    workspace_id: int,
    user_id: int,
    mapped_external_ids: list[str],
    *,
    exclude_group_id: int | None = None,
    exclude_external_id: str | None = None,
) -> bool:
    """Return whether another mapped group still grants this membership."""
    candidates = [
        external_id for external_id in mapped_external_ids if external_id != exclude_external_id
    ]
    if not candidates:
        return False
    query = (
        db.session.query(ScimGroupMember.id)
        .join(ScimGroup, ScimGroup.id == ScimGroupMember.group_id)
        .filter(ScimGroup.workspace_id == workspace_id)
        .filter(ScimGroupMember.user_id == user_id)
        .filter(ScimGroup.external_id.in_(candidates))
    )
    if exclude_group_id is not None:
        query = query.filter(ScimGroup.id != exclude_group_id)
    return query.first() is not None


def _apply_members(
    workspace_id: int,
    external_id: str,
    *,
    desired_ids: set[int],
    removed_ids: set[int],
    role: str | None,
    actor,
    exclude_group_id: int | None = None,
    exclude_external_id: str | None = None,
) -> None:
    """Apply a mapped role without touching protected or manual roles."""
    if role is not None and role not in MAPPABLE_ROLES:  # defensive: never escalate
        raise ScimError("role must be viewer or contributor.")

    moment = _now()
    applied = 0
    revoked = 0
    for user_id in sorted(desired_ids):
        if role is None:
            continue
        member = _find_member(workspace_id, user_id)
        if member is None:
            db.session.add(
                WorkspaceMember(
                    workspace_id=workspace_id,
                    user_id=user_id,
                    role=role,
                    status=STATUS_ACTIVE,
                    provisioning_source=PROVISIONING_SCIM,
                    joined_at=moment,
                )
            )
            applied += 1
            continue
        if member.role in PROTECTED_ROLES:
            continue
        if member.provisioning_source == PROVISIONING_SCIM:
            if member.role != role:
                member.role = role
                applied += 1
        elif ROLE_RANK.get(role, -1) > ROLE_RANK.get(member.role, -1):
            member.role = role
            applied += 1
        if member.status != STATUS_ACTIVE:
            member.status = STATUS_ACTIVE
            member.removed_at = None

    mapped_external_ids = [
        row.group_external_id
        for row in GroupRoleMapping.query.filter_by(workspace_id=workspace_id).all()
    ]
    for user_id in sorted(removed_ids - desired_ids):
        member = _find_member(workspace_id, user_id)
        if member is None or member.provisioning_source != PROVISIONING_SCIM:
            continue
        if _still_mapped(
            workspace_id,
            user_id,
            mapped_external_ids,
            exclude_group_id=exclude_group_id,
            exclude_external_id=exclude_external_id,
        ):
            continue
        if member.status != STATUS_REMOVED:
            member.status = STATUS_REMOVED
            member.removed_at = moment
            revoked += 1

    if applied or revoked:
        audit.record(
            audit.SCIM_ROLE_APPLIED,
            workspace_id=workspace_id,
            user=actor,
            target_type="scim_group",
            target_id=None,
            metadata={
                "group": external_id,
                "role": role or "none",
                "applied": applied,
                "revoked": revoked,
            },
        )


def _mapping_role(workspace_id: int, external_id: str) -> str | None:
    mapping = GroupRoleMapping.query.filter_by(
        workspace_id=workspace_id, group_external_id=external_id
    ).first()
    return mapping.role if mapping is not None else None


def _apply_group(group: ScimGroup, *, actor) -> None:
    role = _mapping_role(group.workspace_id, group.external_id)
    desired_ids = {row.user_id for row in group.member_rows}
    _apply_members(
        group.workspace_id,
        group.external_id,
        desired_ids=desired_ids,
        removed_ids=set(),
        role=role,
        actor=actor,
    )


def _reapply_external_id(workspace_id: int, external_id: str, *, actor) -> None:
    role = _mapping_role(workspace_id, external_id)
    for group in ScimGroup.query.filter_by(
        workspace_id=workspace_id, external_id=external_id
    ).all():
        _apply_members(
            workspace_id,
            external_id,
            desired_ids={row.user_id for row in group.member_rows},
            removed_ids=set(),
            role=role,
            actor=actor,
        )


def _revoke_external_id(
    workspace_id: int, external_id: str, *, actor, exclude_external_id: str | None = None
) -> None:
    for group in ScimGroup.query.filter_by(
        workspace_id=workspace_id, external_id=external_id
    ).all():
        _apply_members(
            workspace_id,
            external_id,
            desired_ids=set(),
            removed_ids={row.user_id for row in group.member_rows},
            role=None,
            actor=actor,
            exclude_external_id=exclude_external_id,
        )


def _serialize_group(group: ScimGroup) -> dict:
    members = []
    for row in group.member_rows:
        members.append(
            {
                "value": str(row.user_id),
                "display": row.user.username if row.user is not None else None,
            }
        )
    return {
        "schemas": [SCIM_GROUP_SCHEMA],
        "id": str(group.id),
        "externalId": group.external_id,
        "displayName": group.display_name,
        "members": members,
        "meta": {"resourceType": "Group"},
    }


# -- Role-mapping CRUD -----------------------------------------------------


def list_mappings(workspace_id, *, actor) -> list[dict]:
    """Return all group-to-role mappings for one organization."""
    _workspace(workspace_id)
    _require_admin(workspace_id, actor)
    rows = (
        GroupRoleMapping.query.filter_by(workspace_id=workspace_id)
        .order_by(GroupRoleMapping.group_external_id)
        .all()
    )
    return [row.to_dict() for row in rows]


def create_mapping(workspace_id, *, actor, group_external_id, role) -> dict:
    """Create a mapping to a safe role and apply it to existing groups."""
    _workspace(workspace_id)
    _require_admin(workspace_id, actor)
    external_id = validate_external_id(group_external_id)
    safe_role = validate_role(role)
    duplicate = GroupRoleMapping.query.filter_by(
        workspace_id=workspace_id, group_external_id=external_id
    ).first()
    if duplicate is not None:
        raise ScimConflict("That group already has a role mapping.")
    row = GroupRoleMapping(
        workspace_id=workspace_id,
        group_external_id=external_id,
        role=safe_role,
        created_by_id=actor.id,
        updated_by_id=actor.id,
    )
    db.session.add(row)
    db.session.flush()
    _reapply_external_id(workspace_id, external_id, actor=actor)
    audit.record(
        audit.SCIM_ROLE_MAPPING_CREATED,
        workspace_id=workspace_id,
        user=actor,
        target_type="group_role_mapping",
        target_id=row.id,
        metadata={"group": external_id, "role": safe_role},
    )
    db.session.commit()
    return row.to_dict()


def update_mapping(workspace_id, mapping_id, *, actor, role=None, group_external_id=None) -> dict:
    """Update a mapping and reapply both the old and new group roles."""
    _workspace(workspace_id)
    _require_admin(workspace_id, actor)
    row = _get_mapping(workspace_id, mapping_id)
    old_external_id = row.group_external_id
    new_external_id = (
        validate_external_id(group_external_id)
        if group_external_id is not None
        else old_external_id
    )
    if role is not None:
        row.role = validate_role(role)
    if new_external_id != old_external_id:
        duplicate = GroupRoleMapping.query.filter_by(
            workspace_id=workspace_id, group_external_id=new_external_id
        ).first()
        if duplicate is not None:
            raise ScimConflict("That group already has a role mapping.")
        row.group_external_id = new_external_id
    row.updated_by_id = actor.id
    db.session.flush()
    if new_external_id != old_external_id:
        _revoke_external_id(workspace_id, old_external_id, actor=actor)
    _reapply_external_id(workspace_id, new_external_id, actor=actor)
    audit.record(
        audit.SCIM_ROLE_MAPPING_UPDATED,
        workspace_id=workspace_id,
        user=actor,
        target_type="group_role_mapping",
        target_id=row.id,
        metadata={"group": new_external_id, "role": row.role},
    )
    db.session.commit()
    return row.to_dict()


def delete_mapping(workspace_id, mapping_id, *, actor) -> None:
    """Delete a mapping and revoke roles granted through its groups."""
    _workspace(workspace_id)
    _require_admin(workspace_id, actor)
    row = _get_mapping(workspace_id, mapping_id)
    external_id = row.group_external_id
    _revoke_external_id(workspace_id, external_id, actor=actor, exclude_external_id=external_id)
    audit.record(
        audit.SCIM_ROLE_MAPPING_DELETED,
        workspace_id=workspace_id,
        user=actor,
        target_type="group_role_mapping",
        target_id=row.id,
        metadata={"group": external_id, "role": row.role},
    )
    db.session.delete(row)
    db.session.commit()


# -- Minimal SCIM Groups ---------------------------------------------------


def list_groups(workspace_id, *, actor) -> list[dict]:
    """List synchronized groups for one organization."""
    _workspace(workspace_id)
    _require_admin(workspace_id, actor)
    rows = (
        ScimGroup.query.filter_by(workspace_id=workspace_id).order_by(ScimGroup.external_id).all()
    )
    return [_serialize_group(row) for row in rows]


def get_group(group_id, *, actor) -> dict:
    """Return one synchronized group."""
    row = _get_group(group_id)
    _require_admin(row.workspace_id, actor)
    return _serialize_group(row)


def create_group(workspace_id, *, actor, payload) -> dict:
    """Create a group from a SCIM-shaped payload and apply its mapping."""
    _workspace(workspace_id)
    _require_admin(workspace_id, actor)
    data = _payload_object(payload)
    external_id = validate_external_id(data.get("externalId"))
    display_name = validate_display_name(data.get("displayName"))
    member_ids = _resolve_user_ids(workspace_id, data.get("members"))
    duplicate = ScimGroup.query.filter_by(
        workspace_id=workspace_id, external_id=external_id
    ).first()
    if duplicate is not None:
        raise ScimConflict("That group already exists.")
    row = ScimGroup(workspace_id=workspace_id, external_id=external_id, display_name=display_name)
    db.session.add(row)
    db.session.flush()
    for user_id in member_ids:
        db.session.add(ScimGroupMember(group_id=row.id, user_id=user_id))
    db.session.flush()
    _apply_group(row, actor=actor)
    audit.record(
        audit.SCIM_GROUP_CREATED,
        workspace_id=workspace_id,
        user=actor,
        target_type="scim_group",
        target_id=row.id,
        metadata={"group": external_id, "members": len(member_ids)},
    )
    db.session.commit()
    return _serialize_group(row)


def replace_group(group_id, *, actor, payload) -> dict:
    """Replace a group's display name and complete member list."""
    row = _get_group(group_id)
    _require_admin(row.workspace_id, actor)
    data = _payload_object(payload)
    row.display_name = validate_display_name(data.get("displayName"))
    new_external_id = validate_external_id(data.get("externalId", row.external_id))
    if new_external_id != row.external_id:
        duplicate = ScimGroup.query.filter_by(
            workspace_id=row.workspace_id, external_id=new_external_id
        ).first()
        if duplicate is not None:
            raise ScimConflict("That group already exists.")
    old_external_id = row.external_id
    row.external_id = new_external_id
    old_ids = {member.user_id for member in row.member_rows}
    new_ids = set(_resolve_user_ids(row.workspace_id, data.get("members")))
    for member in list(row.member_rows):
        db.session.delete(member)
    db.session.flush()
    for user_id in new_ids:
        db.session.add(ScimGroupMember(group_id=row.id, user_id=user_id))
    db.session.flush()
    if old_external_id != new_external_id:
        _revoke_external_id(row.workspace_id, old_external_id, actor=actor)
    _apply_members(
        row.workspace_id,
        new_external_id,
        desired_ids=new_ids,
        removed_ids=old_ids,
        role=_mapping_role(row.workspace_id, new_external_id),
        actor=actor,
    )
    audit.record(
        audit.SCIM_GROUP_UPDATED,
        workspace_id=row.workspace_id,
        user=actor,
        target_type="scim_group",
        target_id=row.id,
        metadata={"group": new_external_id, "members": len(new_ids)},
    )
    db.session.commit()
    return _serialize_group(row)


def patch_group(group_id, *, actor, payload) -> dict:
    """Apply a minimal SCIM PATCH (member add/remove/replace, displayName)."""
    row = _get_group(group_id)
    _require_admin(row.workspace_id, actor)
    data = _payload_object(payload)
    current_ids = {member.user_id for member in row.member_rows}
    desired_ids = set(current_ids)
    display_name = row.display_name

    operations = data.get("Operations")
    if operations is None:
        if "members" in data:
            desired_ids = set(_resolve_user_ids(row.workspace_id, data.get("members")))
        if "displayName" in data:
            display_name = validate_display_name(data.get("displayName"))
    elif not isinstance(operations, list):
        raise ScimError("Operations must be a list.")
    else:
        for operation in operations:
            if not isinstance(operation, dict):
                raise ScimError("Every operation must be an object.")
            op = str(operation.get("op", "")).lower()
            path = str(operation.get("path", "")).strip().lower()
            if path in {"displayname", "displayname.value"}:
                if op not in {"add", "replace"}:
                    raise ScimError("displayName supports add and replace only.")
                display_name = validate_display_name(operation.get("value"))
            elif path in {"members", "members.value"}:
                values = operation.get("value")
                if not isinstance(values, list):
                    raise ScimError("member operations require a list value.")
                ids = set(_resolve_user_ids(row.workspace_id, values))
                if op == "add":
                    desired_ids |= ids
                elif op == "remove":
                    desired_ids -= ids
                elif op == "replace":
                    desired_ids = ids
                else:
                    raise ScimError("members supports add, remove, and replace only.")
            else:
                raise ScimError("Unsupported SCIM operation path.")

    row.display_name = validate_display_name(display_name)
    for member in list(row.member_rows):
        if member.user_id not in desired_ids:
            db.session.delete(member)
    db.session.flush()
    existing_ids = {member.user_id for member in row.member_rows}
    for user_id in desired_ids - existing_ids:
        db.session.add(ScimGroupMember(group_id=row.id, user_id=user_id))
    db.session.flush()
    _apply_members(
        row.workspace_id,
        row.external_id,
        desired_ids=desired_ids,
        removed_ids=current_ids,
        role=_mapping_role(row.workspace_id, row.external_id),
        actor=actor,
    )
    audit.record(
        audit.SCIM_GROUP_UPDATED,
        workspace_id=row.workspace_id,
        user=actor,
        target_type="scim_group",
        target_id=row.id,
        metadata={"group": row.external_id, "members": len(desired_ids)},
    )
    db.session.commit()
    return _serialize_group(row)


def delete_group(group_id, *, actor) -> None:
    """Delete a group and revoke only the roles it granted."""
    row = _get_group(group_id)
    _require_admin(row.workspace_id, actor)
    member_ids = {member.user_id for member in row.member_rows}
    _apply_members(
        row.workspace_id,
        row.external_id,
        desired_ids=set(),
        removed_ids=member_ids,
        role=None,
        actor=actor,
        exclude_group_id=row.id,
    )
    audit.record(
        audit.SCIM_GROUP_DELETED,
        workspace_id=row.workspace_id,
        user=actor,
        target_type="scim_group",
        target_id=row.id,
        metadata={"group": row.external_id},
    )
    db.session.delete(row)
    db.session.commit()


__all__ = [
    "PROVISIONING_MANUAL",
    "PROVISIONING_SCIM",
    "SCIM_GROUP_SCHEMA",
    "ScimConflict",
    "ScimError",
    "ScimNotFound",
    "ScimPermissionDenied",
    "create_group",
    "create_mapping",
    "delete_group",
    "delete_mapping",
    "get_group",
    "list_groups",
    "list_mappings",
    "patch_group",
    "replace_group",
    "update_mapping",
    "validate_display_name",
    "validate_external_id",
    "validate_role",
]
