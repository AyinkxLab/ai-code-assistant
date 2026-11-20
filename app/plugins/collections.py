"""Marketplace collection routes: read surfaces and admin curation (#694).

Any signed-in user can browse curated collections. Only site admins (the
maintainers of the marketplace) may create, edit, feature or delete one. The
JSON API returns structured errors and enforces permission independently of the
HTML UI; the forms reuse the same service validation.
"""

from __future__ import annotations

from functools import wraps

from flask import flash, jsonify, redirect, render_template, request, url_for
from flask_login import current_user, login_required

from app.models import Plugin
from app.plugins import bp
from app.services.plugin_collections import (
    CollectionError,
    collection_error_payload,
    create_collection,
    delete_collection,
    get_collection,
    list_collections,
    serialize_collection,
    update_collection,
)


def _admin_api_required(view):
    """Return structured 401/403 JSON for the curation API."""

    @wraps(view)
    def wrapped(*args, **kwargs):
        if not current_user.is_authenticated:
            return jsonify({"error": "Authentication required.", "code": "unauthorized"}), 401
        if not current_user.is_admin:
            return jsonify({"error": "Admin access required.", "code": "forbidden"}), 403
        return view(*args, **kwargs)

    return wrapped


def _admin_page_required():
    """Return the rendered 403 page for a non-admin curation UI request."""
    return render_template("errors/403.html"), 403


def _collection_data():
    """Read a collection payload from JSON or a form."""
    if request.is_json:
        return request.get_json(silent=True) or {}
    extra = request.form.get("plugin_ids_text", "")
    plugin_ids = request.form.getlist("plugin_ids") + [
        item.strip() for item in extra.replace(",", "\n").splitlines() if item.strip()
    ]
    return {
        "name": request.form.get("name"),
        "slug": request.form.get("slug") or None,
        "description": request.form.get("description") or "",
        "featured": request.form.get("featured") in {"1", "true", "on", "yes"},
        "plugin_ids": plugin_ids,
    }


def _form_error(exc: CollectionError):
    return (
        render_template(
            "plugins/collections_manage.html",
            collections=[serialize_collection(item) for item in list_collections()],
            plugins=Plugin.query.order_by(Plugin.name, Plugin.id).all(),
            error=str(exc),
        ),
        400,
    )


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@bp.route("/collections")
@login_required
def collections_page():
    """Browse featured and curated collections (any signed-in user)."""
    items = [serialize_collection(item, with_plugins=True) for item in list_collections()]
    return render_template(
        "plugins/collections.html",
        collections=items,
        featured=[item for item in items if item["featured"]],
    )


@bp.route("/collections/manage")
@login_required
def collections_manage():
    """Admin-only curation UI."""
    if not current_user.is_admin:
        return _admin_page_required()
    return render_template(
        "plugins/collections_manage.html",
        collections=[serialize_collection(item) for item in list_collections()],
        plugins=Plugin.query.order_by(Plugin.name, Plugin.id).all(),
        error=None,
    )


@bp.route("/collections/manage", methods=["POST"])
@login_required
def collections_create_form():
    """Create a collection from the curation form."""
    if not current_user.is_admin:
        return _admin_page_required()
    try:
        create_collection(_collection_data(), actor_id=current_user.id)
    except CollectionError as exc:
        return _form_error(exc)
    flash("Collection created.", "success")
    return redirect(url_for("plugins.collections_manage"))


@bp.route("/collections/<int:collection_id>/feature", methods=["POST"])
@login_required
def collections_toggle_featured(collection_id: int):
    """Toggle a collection's featured flag from the curation UI."""
    if not current_user.is_admin:
        return _admin_page_required()
    try:
        collection = get_collection(collection_id)
        update_collection(collection, {"featured": not collection.featured})
    except CollectionError as exc:
        return _form_error(exc)
    return redirect(url_for("plugins.collections_manage"))


@bp.route("/collections/<int:collection_id>/delete", methods=["POST"])
@login_required
def collections_delete_form(collection_id: int):
    """Delete a collection from the curation UI."""
    if not current_user.is_admin:
        return _admin_page_required()
    try:
        delete_collection(get_collection(collection_id))
    except CollectionError as exc:
        return _form_error(exc)
    flash("Collection deleted.", "success")
    return redirect(url_for("plugins.collections_manage"))


# --------------------------------------------------------------------------
# JSON API
# --------------------------------------------------------------------------


@bp.route("/api/collections", methods=["GET"])
@login_required
def api_list_collections():
    """List collections; ``?featured=true`` narrows to featured ones."""
    featured_only = request.args.get("featured", "").lower() in {"1", "true", "yes"}
    items = [
        serialize_collection(item, with_plugins=True)
        for item in list_collections(featured_only=featured_only)
    ]
    return jsonify({"collections": items, "count": len(items)})


@bp.route("/api/collections/<string:identifier>", methods=["GET"])
@login_required
def api_get_collection(identifier: str):
    """Return one collection by slug or numeric id."""
    try:
        collection = get_collection(identifier)
    except CollectionError as exc:
        return jsonify(collection_error_payload(exc)), exc.status
    return jsonify(serialize_collection(collection, with_plugins=True))


@bp.route("/api/collections", methods=["POST"])
@login_required
@_admin_api_required
def api_create_collection():
    """Create a curated collection (admin only)."""
    try:
        collection = create_collection(_collection_data(), actor_id=current_user.id)
    except CollectionError as exc:
        return jsonify(collection_error_payload(exc)), exc.status
    return jsonify(serialize_collection(collection, with_plugins=True)), 201


@bp.route("/api/collections/<int:collection_id>", methods=["PUT", "PATCH"])
@login_required
@_admin_api_required
def api_update_collection(collection_id: int):
    """Update name, description, featured flag, slug or plugin list (admin only)."""
    try:
        collection = get_collection(collection_id)
        update_collection(collection, request.get_json(silent=True) or {})
    except CollectionError as exc:
        return jsonify(collection_error_payload(exc)), exc.status
    return jsonify(serialize_collection(collection, with_plugins=True))


@bp.route("/api/collections/<int:collection_id>", methods=["DELETE"])
@login_required
@_admin_api_required
def api_delete_collection(collection_id: int):
    """Delete a curated collection (admin only)."""
    try:
        delete_collection(get_collection(collection_id))
    except CollectionError as exc:
        return jsonify(collection_error_payload(exc)), exc.status
    return "", 204
