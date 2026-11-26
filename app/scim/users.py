"""SCIM 2.0 User resource mapping and operations (#592).

Users are application accounts. A SCIM-created user gets a random, unusable
password and an active membership in the token's default workspace. Deactivation
is global (``User.is_active = False``) and revokes every tracked browser
session; it is deliberately documented as global because the application has one
login identity shared by all workspaces.

Only fixed SCIM error details are used — no emails, ids, token values, or raw
exception text is returned to a provisioning client.
"""

from __future__ import annotations

import re
import secrets

from app.extensions import db
from app.models import User, Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_CONTRIBUTOR, STATUS_ACTIVE
from app.scim.errors import (
    SCIM_TYPE_INVALID_FILTER,
    SCIM_TYPE_INVALID_SYNTAX,
    SCIM_TYPE_INVALID_VALUE,
    SCIM_TYPE_UNIQUENESS,
    ScimError,
)
from app.services import user_sessions

USER_SCHEMA = "urn:ietf:params:scim:schemas:core:2.0:User"
LIST_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:ListResponse"
PATCH_SCHEMA = "urn:ietf:params:scim:api:messages:2.0:PatchOp"

DEFAULT_START_INDEX = 1
DEFAULT_COUNT = 50
MAX_COUNT = 200

_FILTER_RE = re.compile(
    r"^\s*(userName|emails\.value)\s+eq\s+(?:\"([^\"]*)\"|'([^']*)')\s*$",
    re.IGNORECASE,
)
_PATH_FIELD = {
    "active": "active",
    "username": "userName",
    "displayname": "displayName",
    "emails": "emails",
    "emails.value": "emails",
}


def to_scim_user(user: User) -> dict:
    """Map an application user to a minimal SCIM 2.0 User resource."""
    return {
        "schemas": [USER_SCHEMA],
        "id": str(user.id),
        "userName": user.username,
        "displayName": user.username,
        "emails": [{"value": user.email, "primary": True}],
        "active": bool(user.is_active),
        "meta": {"resourceType": "User", "location": f"/scim/v2/Users/{user.id}"},
    }


def workspace_users(owner_id: int, workspace_id: int) -> list[User]:
    """Return the organization owner plus active members of the workspace."""
    workspace = db.session.get(Workspace, workspace_id)
    if workspace is None or workspace.user_id != owner_id:
        raise ScimError(404, "The provisioning workspace was not found.")
    ids = {owner_id}
    ids.update(
        row.user_id
        for row in WorkspaceMember.query.filter_by(
            workspace_id=workspace_id, status=STATUS_ACTIVE
        ).all()
    )
    return User.query.filter(User.id.in_(ids)).order_by(User.id.asc()).all()


def get_user(owner_id: int, workspace_id: int, user_id: str) -> User:
    """Return a user visible to the provisioning workspace or raise 404."""
    try:
        target = int(user_id)
    except (TypeError, ValueError) as exc:
        raise ScimError(404, "The requested user was not found.") from exc
    for user in workspace_users(owner_id, workspace_id):
        if user.id == target:
            return user
    raise ScimError(404, "The requested user was not found.")


def parse_filter(expression: str | None) -> tuple[str, str] | None:
    """Parse the supported ``eq`` filters; reject everything else."""
    if expression is None or not expression.strip():
        return None
    match = _FILTER_RE.match(expression)
    if match is None:
        raise ScimError(
            400,
            "Only userName eq and emails.value eq filters are supported.",
            SCIM_TYPE_INVALID_FILTER,
        )
    field = "userName" if match.group(1).lower() == "username" else "emails.value"
    value = match.group(2) if match.group(2) is not None else match.group(3)
    return field, value


def parse_pagination(start_index, count) -> tuple[int, int]:
    """Validate and canonicalize SCIM pagination parameters."""
    try:
        start = DEFAULT_START_INDEX if start_index in (None, "") else int(start_index)
        size = DEFAULT_COUNT if count in (None, "") else int(count)
    except (TypeError, ValueError) as exc:
        raise ScimError(
            400, "startIndex and count must be integers.", SCIM_TYPE_INVALID_VALUE
        ) from exc
    if start < 1 or size < 1 or size > MAX_COUNT:
        raise ScimError(
            400,
            f"startIndex must be at least 1 and count must be between 1 and {MAX_COUNT}.",
            SCIM_TYPE_INVALID_VALUE,
        )
    return start, size


def _validate_schemas(payload, required: str) -> None:
    schemas = payload.get("schemas")
    if not isinstance(schemas, list) or required not in schemas:
        raise ScimError(400, f"schemas must include {required}.", SCIM_TYPE_INVALID_SYNTAX)


def _normalize_username(value) -> str:
    username = str(value or "").strip()
    if not username or len(username) > 80:
        raise ScimError(
            400, "userName is required (at most 80 characters).", SCIM_TYPE_INVALID_VALUE
        )
    return username


def _extract_email(value) -> str:
    if isinstance(value, str):
        candidates = [value]
    elif isinstance(value, list):
        candidates = [entry.get("value") if isinstance(entry, dict) else entry for entry in value]
    else:
        candidates = []
    email = next((str(item).strip() for item in candidates if str(item or "").strip()), "")
    if "@" not in email or len(email) > 255:
        raise ScimError(400, "A valid primary email address is required.", SCIM_TYPE_INVALID_VALUE)
    return email


def _check_unique(username: str, email: str, *, exclude_id: int | None = None) -> None:
    for field, value in (("username", username), ("email", email)):
        query = User.query.filter_by(**{field: value})
        if exclude_id is not None:
            query = query.filter(User.id != exclude_id)
        if query.first() is not None:
            raise ScimError(409, "The userName or email is already in use.", SCIM_TYPE_UNIQUENESS)


def create_user(owner_id: int, workspace_id: int, payload: dict) -> User:
    """Create a user and add an active member to the target workspace."""
    _validate_schemas(payload, USER_SCHEMA)
    username = _normalize_username(payload.get("userName"))
    email = _extract_email(payload.get("emails"))
    _check_unique(username, email)

    user = User(username=username, email=email)
    # SCIM-created accounts have no usable password; sign-in requires the
    # identity provider (or an operator reset). The generated value is never
    # returned or stored in plain text.
    user.set_password(secrets.token_urlsafe(32))
    db.session.add(user)
    db.session.flush()
    db.session.add(
        WorkspaceMember(
            workspace_id=workspace_id,
            user_id=user.id,
            role=ROLE_CONTRIBUTOR,
            status=STATUS_ACTIVE,
        )
    )
    db.session.commit()
    return user


def _deactivate(user: User) -> None:
    user.is_active = False
    user_sessions.revoke_all_for_user(user, reason="scim_deactivation", commit=False)


def _apply_value(user: User, values: dict) -> None:
    if "userName" in values:
        username = _normalize_username(values["userName"])
        _check_unique(username, user.email, exclude_id=user.id)
        user.username = username
    if "emails" in values:
        email = _extract_email(values["emails"])
        _check_unique(user.username, email, exclude_id=user.id)
        user.email = email
    if "active" in values:
        active = values["active"]
        if not isinstance(active, bool):
            raise ScimError(400, "active must be a boolean.", SCIM_TYPE_INVALID_VALUE)
        if active:
            user.is_active = True
        else:
            _deactivate(user)


def update_user(user: User, payload: dict) -> User:
    """Apply a SCIM PATCH request to one user."""
    _validate_schemas(payload, PATCH_SCHEMA)
    operations = payload.get("Operations", payload.get("operations"))
    if not isinstance(operations, list) or not operations:
        raise ScimError(400, "At least one PATCH operation is required.", SCIM_TYPE_INVALID_SYNTAX)
    for operation in operations:
        if not isinstance(operation, dict):
            raise ScimError(400, "PATCH operations must be objects.", SCIM_TYPE_INVALID_SYNTAX)
        op = str(operation.get("op", "")).strip().lower()
        if op not in {"add", "replace"}:
            raise ScimError(
                400,
                "Only add and replace PATCH operations are supported.",
                SCIM_TYPE_INVALID_SYNTAX,
            )
        value = operation.get("value")
        if not isinstance(value, dict):
            path = str(operation.get("path", "")).lower()
            field = _PATH_FIELD.get(path)
            if field is None:
                raise ScimError(400, "Unsupported PATCH path.", SCIM_TYPE_INVALID_SYNTAX)
            value = {field: value}
        _apply_value(user, value)
    db.session.commit()
    return user


def deactivate_user(user: User) -> User:
    """Deactivate a user (SCIM DELETE) and revoke tracked sessions."""
    _deactivate(user)
    db.session.commit()
    return user


def list_users(
    owner_id: int,
    workspace_id: int,
    filter_expression: str | None,
    start_index,
    count,
) -> dict:
    """Return a filtered, paginated SCIM ListResponse."""
    parsed = parse_filter(filter_expression)
    start, size = parse_pagination(start_index, count)
    rows = workspace_users(owner_id, workspace_id)
    if parsed is not None:
        field, value = parsed
        if field == "userName":
            rows = [user for user in rows if user.username == value]
        else:
            rows = [user for user in rows if user.email == value]
    total = len(rows)
    selected = rows[start - 1 : start - 1 + size]
    return {
        "schemas": [LIST_SCHEMA],
        "totalResults": total,
        "startIndex": start,
        "itemsPerPage": len(selected),
        "Resources": [to_scim_user(user) for user in selected],
    }


__all__ = [
    "DEFAULT_COUNT",
    "DEFAULT_START_INDEX",
    "LIST_SCHEMA",
    "MAX_COUNT",
    "PATCH_SCHEMA",
    "USER_SCHEMA",
    "create_user",
    "deactivate_user",
    "get_user",
    "list_users",
    "parse_filter",
    "parse_pagination",
    "to_scim_user",
    "update_user",
    "workspace_users",
]
