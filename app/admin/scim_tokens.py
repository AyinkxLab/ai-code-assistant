"""SCIM bearer token management for organizations (#594).

Tokens are organization-scoped, hash-only, and the secret is returned exactly
once by the issue/rotate response. Listing, rotation, and revocation are
metadata-only. All failures use the structured catalog response.
"""

from __future__ import annotations

from datetime import UTC, datetime

from flask import jsonify, request
from flask_login import current_user, login_required

from app.admin import bp
from app.admin.organizations_common import action_payload, resolve_organization
from app.api.error_catalog import NOT_FOUND, error_response
from app.extensions import db
from app.services import audit
from app.services.scim_tokens import (
    ScimTokenError,
    create_token,
    list_tokens,
    revoke_token,
    rotate_token,
)


def _payload(row, secret=None) -> dict:
    body = {"token": row.to_dict()}
    if secret is not None:
        body["secret"] = secret
    return body


def _parse_expiry(value):
    """Parse an optional ISO-8601 expiry into a naive UTC datetime."""
    if not value:
        return None
    try:
        parsed = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError as exc:
        raise ScimTokenError("expires_at must be an ISO-8601 timestamp.") from exc
    if parsed.tzinfo is not None:
        parsed = parsed.astimezone(UTC).replace(tzinfo=None)
    return parsed


@bp.route("/organizations/<int:workspace_id>/scim-tokens", methods=["GET", "POST"])
@login_required
def scim_tokens(workspace_id: int):
    """List organization SCIM tokens or issue a new one."""
    workspace, error = resolve_organization(workspace_id)
    if error is not None:
        return error
    owner_id = workspace.user_id

    if request.method == "GET":
        return jsonify({"tokens": [row.to_dict() for row in list_tokens(owner_id)]})

    data = action_payload()
    try:
        row, secret = create_token(
            owner_id,
            workspace.id,
            created_by_id=current_user.id,
            label=data.get("label"),
            expires_at=_parse_expiry(data.get("expires_at")),
        )
    except ScimTokenError as exc:
        return error_response(exc.code, str(exc))
    audit.record(
        audit.SCIM_TOKEN_ISSUED,
        workspace_id=workspace.id,
        user=current_user,
        target_type="scim_token",
        target_id=row.id,
        metadata={"prefix": row.prefix},
    )
    db.session.commit()
    return jsonify(_payload(row, secret)), 201


@bp.route(
    "/organizations/<int:workspace_id>/scim-tokens/<int:token_id>",
    methods=["PATCH", "DELETE"],
)
@login_required
def scim_token_detail(workspace_id: int, token_id: int):
    """Rotate or revoke one organization SCIM token."""
    workspace, error = resolve_organization(workspace_id)
    if error is not None:
        return error
    owner_id = workspace.user_id

    if request.method == "DELETE":
        try:
            row = revoke_token(owner_id, token_id, revoked_by_id=current_user.id)
        except ScimTokenError as exc:
            return error_response(exc.code, str(exc))
        if row is None:
            return error_response(NOT_FOUND, "SCIM token not found.")
        audit.record(
            audit.SCIM_TOKEN_REVOKED,
            workspace_id=workspace.id,
            user=current_user,
            target_type="scim_token",
            target_id=row.id,
            metadata={"prefix": row.prefix},
        )
        db.session.commit()
        return jsonify({"token": row.to_dict()})

    try:
        result = rotate_token(owner_id, token_id, rotated_by_id=current_user.id)
    except ScimTokenError as exc:
        return error_response(exc.code, str(exc))
    if result is None:
        return error_response(NOT_FOUND, "SCIM token not found.")
    row, secret = result
    audit.record(
        audit.SCIM_TOKEN_ROTATED,
        workspace_id=workspace.id,
        user=current_user,
        target_type="scim_token",
        target_id=row.id,
        metadata={"prefix": row.prefix},
    )
    db.session.commit()
    return jsonify(_payload(row, secret))
