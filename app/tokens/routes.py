"""API token rotation and revocation UI and JSON API (#617).

Every response is metadata-only: the secret is returned exactly once from
create/rotate and is never part of a list, detail, or audit record. Revocation
keeps the row for audit history.
"""

from __future__ import annotations

from flask import jsonify, render_template, request
from flask_login import current_user, login_required

from app.api.error_catalog import (
    CONFLICT,
    NOT_FOUND,
    error_response,
)
from app.extensions import db
from app.services import api_tokens, audit
from app.tokens import bp


def _payload(row, secret=None):
    body = {"token": row.to_dict()}
    if secret is not None:
        body["secret"] = secret
    return body


@bp.route("/")
@login_required
def index():
    """Render the token management page."""
    return render_template("tokens/index.html")


@bp.route("/api", methods=["GET"])
@login_required
def list_tokens():
    """Return metadata for the caller's tokens (never the secret)."""
    return jsonify([row.to_dict() for row in api_tokens.list_tokens(current_user)])


@bp.route("/api", methods=["POST"])
@login_required
def create_token():
    """Create a token and return its secret once."""
    data = request.get_json(silent=True) or {}
    try:
        row, secret = api_tokens.create_token(
            current_user,
            label=data.get("label"),
            scopes=data.get("scopes"),
        )
    except api_tokens.ApiTokenError as exc:
        return error_response(exc.code, str(exc))
    audit.record(
        audit.API_TOKEN_CREATED,
        user=current_user,
        target_type="api_token",
        target_id=row.id,
        metadata={"label": row.label, "scopes": row.scope_list},
    )
    db.session.commit()
    return jsonify(_payload(row, secret)), 201


@bp.route("/api/<int:token_id>/rotate", methods=["POST"])
@login_required
def rotate_token(token_id: int):
    """Replace a token's secret, returning the new secret once."""
    try:
        result = api_tokens.rotate_token(current_user, token_id)
    except api_tokens.ApiTokenError as exc:
        return error_response(exc.code, str(exc))
    if result is None:
        return error_response(NOT_FOUND)
    row, secret = result
    audit.record(
        audit.API_TOKEN_ROTATED,
        user=current_user,
        target_type="api_token",
        target_id=row.id,
        metadata={"label": row.label, "scopes": row.scope_list},
    )
    db.session.commit()
    return jsonify(_payload(row, secret))


@bp.route("/api/<int:token_id>", methods=["DELETE"])
@login_required
def revoke_token(token_id: int):
    """Revoke a token after the UI confirmation, keeping its audit history."""
    row = api_tokens.get_owned(current_user, token_id)
    if row is None:
        return error_response(NOT_FOUND)
    if row.revoked_at is not None:
        return error_response(CONFLICT, "This token is already revoked.")
    api_tokens.revoke_token(current_user, token_id)
    audit.record(
        audit.API_TOKEN_REVOKED,
        user=current_user,
        target_type="api_token",
        target_id=row.id,
        metadata={"label": row.label, "scopes": row.scope_list},
    )
    db.session.commit()
    return jsonify({"ok": True})
