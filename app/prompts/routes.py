"""Prompt routes: CRUD, favorites, categories, and search."""

from flask import jsonify, render_template, request
from flask_login import current_user, login_required
from sqlalchemy import func

from app.extensions import db
from app.models import Prompt, Workspace, WorkspaceMember
from app.models.workspace_member import ROLE_CONTRIBUTOR, ROLE_OWNER, STATUS_ACTIVE
from app.prompts import bp
from app.services import audit, prompt_import
from app.services import prompt_versions as prompt_versions_service
from app.services.permissions import role_for


def _get_prompt(prompt_id: int) -> Prompt:
    """Return an owned personal prompt or a team prompt the user may manage."""
    prompt = Prompt.query.filter_by(id=prompt_id).first_or_404()
    if not prompt.is_team:
        if prompt.user_id != current_user.id:
            from flask import abort

            abort(404)
        return prompt
    role = role_for(prompt.workspace_id, current_user)
    if role not in {ROLE_OWNER, ROLE_CONTRIBUTOR}:
        from flask import abort

        abort(404)
    return prompt


def _team_workspace_ids():
    """Workspace ids the current user can access through ownership or membership."""
    owned = [row.id for row in Workspace.query.filter_by(user_id=current_user.id).all()]
    joined = [
        row.workspace_id
        for row in WorkspaceMember.query.filter_by(
            user_id=current_user.id, status=STATUS_ACTIVE
        ).all()
    ]
    return sorted(set(owned + joined))


@bp.route("/")
@login_required
def index():
    """Render the prompt management page."""
    return render_template("prompts/index.html")


@bp.route("/api/prompts", methods=["GET"])
@login_required
def list_prompts():
    """Return prompts, optionally filtered by search query, category, or favorite."""
    query = request.args.get("q", "").strip().lower()
    category = request.args.get("category", "").strip()
    favorites_only = request.args.get("favorites") == "1"

    scope = request.args.get("scope", "personal").strip().lower()
    if scope == "team":
        try:
            workspace_id = int(request.args.get("workspace_id", ""))
        except ValueError:
            return jsonify({"error": "A valid workspace_id is required for team prompts."}), 400
        if workspace_id not in _team_workspace_ids():
            return jsonify({"error": "Workspace not found."}), 404
        base = Prompt.query.filter_by(is_team=True, workspace_id=workspace_id)
    elif scope == "personal":
        base = Prompt.query.filter_by(user_id=current_user.id, is_team=False)
    else:
        return jsonify({"error": "scope must be personal or team."}), 400
    if query:
        base = base.filter(
            func.lower(Prompt.title).contains(query)
            | func.lower(Prompt.category).contains(query)
            | func.lower(Prompt.content).contains(query)
        )
    if category:
        base = base.filter(Prompt.category == category)
    if favorites_only:
        base = base.filter(Prompt.is_favorite.is_(True))

    prompts = base.order_by(Prompt.is_favorite.desc(), Prompt.updated_at.desc()).all()
    return jsonify([p.to_dict() for p in prompts])


@bp.route("/api/team-workspaces", methods=["GET"])
@login_required
def list_team_workspaces():
    """List workspaces the caller can browse, with their current role."""
    result = []
    for workspace_id in _team_workspace_ids():
        workspace = db.session.get(Workspace, workspace_id)
        role = role_for(workspace_id, current_user)
        if workspace is not None and role:
            result.append({"id": workspace.id, "name": workspace.name, "role": role})
    return jsonify(result)


@bp.route("/api/categories", methods=["GET"])
@login_required
def list_categories():
    """Return the distinct prompt categories used by the current user."""
    rows = (
        db.session.query(func.distinct(Prompt.category))
        .filter(Prompt.user_id == current_user.id)
        .all()
    )
    return jsonify([row[0] for row in rows])


@bp.route("/api/prompts/export", methods=["GET"])
@login_required
def export_prompts():
    """Export the current user's prompts as a JSON array (import-compatible)."""
    prompts = Prompt.query.filter_by(user_id=current_user.id).order_by(Prompt.id).all()
    payload = [{"title": p.title, "content": p.content, "category": p.category} for p in prompts]
    response = jsonify(payload)
    response.headers["Content-Disposition"] = "attachment; filename=prompts.json"
    return response


@bp.route("/api/prompts/import", methods=["POST"])
@login_required
def import_prompts_route():
    """Bulk import prompts from an uploaded ``.json`` or ``.csv`` file.

    Each row is validated independently; valid rows are imported and invalid
    rows are reported per-row. A payload that cannot be parsed at all is a 400.
    """
    upload = request.files.get("file")
    if upload is None or not upload.filename:
        return jsonify({"error": "A .json or .csv file is required."}), 400
    try:
        result = prompt_import.import_prompts(current_user.id, upload.filename, upload.read())
    except prompt_import.PromptImportError as exc:
        return jsonify({"error": str(exc)}), 400
    return jsonify(result)


@bp.route("/api/prompts", methods=["POST"])
@login_required
def create_prompt():
    """Create a new prompt template."""
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "").strip()
    content = (data.get("content") or "").strip()
    if not title or not content:
        return jsonify({"error": "Both title and content are required."}), 400

    prompt = Prompt(
        user_id=current_user.id,
        title=title[:200],
        content=content,
        category=(data.get("category") or "General").strip()[:80] or "General",
        is_favorite=bool(data.get("is_favorite")),
    )
    if data.get("is_team"):
        try:
            workspace_id = int(data.get("workspace_id"))
        except (TypeError, ValueError):
            return jsonify({"error": "A workspace is required for a team prompt."}), 400
        if workspace_id not in _team_workspace_ids():
            return jsonify({"error": "Workspace not found."}), 404
        if role_for(workspace_id, current_user) not in {ROLE_OWNER, ROLE_CONTRIBUTOR}:
            return (
                jsonify({"error": "Only workspace owners and contributors can add team prompts."}),
                403,
            )
        prompt.is_team = True
        prompt.workspace_id = workspace_id
    db.session.add(prompt)
    db.session.flush()
    prompt_versions_service.record_version(prompt, changed_by=current_user.id)
    db.session.commit()
    return jsonify(prompt.to_dict()), 201


@bp.route("/api/prompts/<int:prompt_id>", methods=["GET"])
@login_required
def get_prompt(prompt_id: int):
    """Return a single prompt."""
    prompt = _get_prompt(prompt_id)
    return jsonify(prompt.to_dict())


@bp.route("/api/prompts/<int:prompt_id>", methods=["PATCH"])
@login_required
def update_prompt(prompt_id: int):
    """Update a prompt's fields, or revert it to a previous version.

    Passing ``revert_to_version`` (a version row id or version number) restores
    that snapshot and records the restore as a new version. Otherwise any
    change to title/content/category is recorded as a new version.
    """
    prompt = _get_prompt(prompt_id)
    data = request.get_json(silent=True) or {}

    if data.get("revert_to_version") is not None:
        return _revert_prompt(prompt, data.get("revert_to_version"))

    changed = False
    if "title" in data:
        title = (data.get("title") or prompt.title).strip()[:200]
        changed = changed or title != prompt.title
        prompt.title = title
    if "content" in data:
        content = (data.get("content") or "").strip()
        if not content:
            return jsonify({"error": "Content cannot be empty."}), 400
        changed = changed or content != prompt.content
        prompt.content = content
    if "category" in data:
        category = (data.get("category") or "General").strip()[:80] or "General"
        changed = changed or category != prompt.category
        prompt.category = category
    if "is_favorite" in data:
        prompt.is_favorite = bool(data["is_favorite"])

    if changed:
        prompt_versions_service.record_version(prompt, changed_by=current_user.id)
    db.session.commit()
    return jsonify(prompt.to_dict())


def _revert_prompt(prompt: Prompt, selector):
    """Restore ``prompt`` to the selected version, recording a new version."""
    version = prompt_versions_service.resolve_version(prompt.id, selector)
    if version is None:
        return jsonify({"error": "Version not found."}), 404
    prompt_versions_service.revert_to_version(prompt, version, changed_by=current_user.id)
    db.session.commit()
    return jsonify(prompt.to_dict())


@bp.route("/api/prompts/<int:prompt_id>/revert", methods=["POST"])
@login_required
def revert_prompt(prompt_id: int):
    """Revert a prompt to a version selected by ``version`` (id or number)."""
    prompt = _get_prompt(prompt_id)
    data = request.get_json(silent=True) or {}
    selector = data.get("version", data.get("revert_to_version"))
    if selector is None:
        return jsonify({"error": "A version is required."}), 400
    return _revert_prompt(prompt, selector)


@bp.route("/api/prompts/<int:prompt_id>/versions", methods=["GET"])
@login_required
def list_prompt_versions(prompt_id: int):
    """Return the prompt's version timeline (oldest first) with unified diffs."""
    prompt = _get_prompt(prompt_id)
    return jsonify(prompt_versions_service.version_timeline(prompt.id))


@bp.route("/api/prompts/<int:prompt_id>/versions/<int:version_id>", methods=["GET"])
@login_required
def get_prompt_version(prompt_id: int, version_id: int):
    """Return a single version, including its diff against the previous one."""
    prompt = _get_prompt(prompt_id)
    version = prompt_versions_service.get_version(prompt.id, version_id)
    if version is None:
        return jsonify({"error": "Version not found."}), 404
    return jsonify(prompt_versions_service.version_payload(version))


@bp.route("/api/prompts/<int:prompt_id>", methods=["DELETE"])
@login_required
def delete_prompt(prompt_id: int):
    """Delete a prompt template, retaining its version history for a period."""
    prompt = _get_prompt(prompt_id)
    prompt_id_value = prompt.id
    title = prompt.title
    prompt_versions_service.mark_prompt_deleted(prompt.id)
    db.session.delete(prompt)
    audit.record(
        audit.PROMPT_DELETED,
        user=current_user,
        target_type="prompt",
        target_id=prompt_id_value,
        metadata={"title": title},
    )
    db.session.commit()
    prompt_versions_service.purge_expired_versions()
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/api/prompts/<int:prompt_id>/favorite", methods=["POST"])
@login_required
def toggle_favorite(prompt_id: int):
    """Toggle the favorite flag on a prompt."""
    prompt = _get_prompt(prompt_id)
    prompt.is_favorite = not prompt.is_favorite
    db.session.commit()
    return jsonify(prompt.to_dict())
