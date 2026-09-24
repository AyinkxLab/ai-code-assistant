"""LLM provider API key routes: settings page and owner-scoped CRUD.

List/detail responses are redacted (metadata only). The plaintext key is
accepted on create, encrypted immediately, and never returned.
"""

from flask import jsonify, render_template, request
from flask_login import current_user, login_required

from app.keys import bp
from app.services import api_keys as api_keys_service
from app.services.api_keys import ApiKeyError


@bp.route("/")
@login_required
def index():
    """Render the API-key management scaffold."""
    return render_template("keys/index.html")


@bp.route("/api/keys", methods=["GET"])
@login_required
def list_keys():
    return jsonify([key.to_dict() for key in api_keys_service.list_keys(current_user)])


@bp.route("/api/keys", methods=["POST"])
@login_required
def create_key():
    data = request.get_json(silent=True) or {}
    try:
        key = api_keys_service.create_key(
            current_user,
            provider=data.get("provider"),
            secret=data.get("key") or data.get("secret"),
            label=data.get("label"),
        )
    except ApiKeyError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(key.to_dict()), 201


@bp.route("/api/keys/<int:key_id>", methods=["PATCH"])
@login_required
def update_key(key_id: int):
    data = request.get_json(silent=True) or {}
    key = api_keys_service.update_key(
        current_user, key_id, label=data.get("label"), is_active=data.get("is_active")
    )
    if key is None:
        return jsonify({"error": "API key not found."}), 404
    return jsonify(key.to_dict())


@bp.route("/api/keys/<int:key_id>", methods=["DELETE"])
@login_required
def delete_key(key_id: int):
    if not api_keys_service.delete_key(current_user, key_id):
        return jsonify({"error": "API key not found."}), 404
    return jsonify({"ok": True})
