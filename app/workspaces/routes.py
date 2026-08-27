"""Workspaces routes: workspace CRUD, project import, and project intelligence.

Layout
------
Pages (HTML)
    /workspaces/                         workspace list
    /workspaces/<id>                     workspace detail (projects)
    /workspaces/<id>/projects/<pid>      project explorer

API (JSON, all scoped to the current user)
    /workspaces/api/workspaces                       list / create
    /workspaces/api/workspaces/<id>                  rename / delete
    /workspaces/api/workspaces/<id>/projects         list / import
    /workspaces/api/projects/<pid>                   delete
    /workspaces/api/projects/<pid>/tree              lazy directory listing
    /workspaces/api/projects/<pid>/file              single file contents
    /workspaces/api/projects/<pid>/search            project search
    /workspaces/api/projects/<pid>/stats             health dashboard
    /workspaces/api/projects/<pid>/messages          chat history
    /workspaces/api/projects/<pid>/chat              AI project chat
    /workspaces/api/projects/<pid>/chat/stream       AI project chat (SSE)
    /workspaces/api/projects/<pid>/analyze           project analysis
    /workspaces/api/workspaces/<id>/members          list (members) / add (owner)
    /workspaces/api/workspaces/<id>/members/<uid>    update role / remove (owner)
    /workspaces/api/workspaces/<id>/transfer         transfer ownership (owner)
    /workspaces/api/workspaces/<id>/membership       self-service leave (member)
    /workspaces/api/workspaces/<id>/heartbeat        presence heartbeat (member)
    /workspaces/api/workspaces/<id>/invitations      invite lifecycle (owner)
    /workspaces/api/workspaces/<id>/activity         activity feed (members)
    /workspaces/api/workspaces/<id>/audit            owner-only audit log
    /workspaces/api/workspaces/<id>/settings         collaboration settings
    /workspaces/api/invitations/<token>/accept|decline  public accept flow
    /workspaces/api/notifications...                 inbox, count, read, prefs
    /workspaces/api/projects/<pid>/comments          project discussion
    /workspaces/invitations/<token>                  public invitation landing
    /workspaces/notifications                        notifications page
    /workspaces/<id>/members                         members page
    /workspaces/<id>/audit                           audit page
"""

import json
from collections import Counter
from pathlib import Path

from flask import Response, jsonify, render_template, request, stream_with_context, url_for
from flask_login import current_user, login_required

from app.extensions import db
from app.models import Project, ProjectMessage, User, Workspace, WorkspaceMember
from app.models.activity_event import (
    EVENT_AI_ANALYSIS_RUN,
    EVENT_MEMBER_ADDED,
    EVENT_MEMBER_REMOVED,
    EVENT_PROJECT_IMPORTED,
    EVENT_ROLE_CHANGED,
)
from app.models.project import SOURCE_ARCHIVE, SOURCE_GITHUB, STATUS_READY
from app.models.workspace_member import (
    MEMBER_ROLES,
    ROLE_OWNER,
    ROLE_VIEWER,
    STATUS_ACTIVE,
)
from app.services import project_analysis
from app.services.activity import record_activity
from app.services.events import emit_event
from app.services.github import (
    GitHubError,
    GitHubInvalidError,
    get_github_client,
    validate_full_name,
)
from app.services.importing import ProjectImportError, extract_archive, import_github_repo
from app.services.invitations import cancel_pending_for_user
from app.services.llm import LLMProviderError, get_provider
from app.services.notifications import notify
from app.services.permissions import resolve_workspace
from app.services.search import search_project
from app.workspaces import bp

MAX_TREE_ENTRIES = 1000


# --------------------------------------------------------------------------
# Ownership helpers
# --------------------------------------------------------------------------


def _get_workspace(workspace_id: int) -> Workspace:
    return Workspace.query.filter_by(id=workspace_id, user_id=current_user.id).first_or_404()


def _get_project(project_id: int) -> Project:
    return Project.query.filter_by(id=project_id, user_id=current_user.id).first_or_404()


def _member_role(workspace_id: int) -> str | None:
    """Return the current user's role in the workspace, or ``None``."""
    if Workspace.query.filter_by(id=workspace_id, user_id=current_user.id).first() is not None:
        return ROLE_OWNER
    membership = WorkspaceMember.query.filter_by(
        workspace_id=workspace_id, user_id=current_user.id
    ).first()
    return membership.role if membership else None


def _get_membership(workspace_id: int, user_id: int) -> WorkspaceMember:
    return WorkspaceMember.query.filter_by(
        workspace_id=workspace_id, user_id=user_id
    ).first_or_404()


def _validate_project_path(path: str) -> str:
    """Validate a project-relative path against traversal attempts."""
    cleaned = (path or "").strip()
    if cleaned.startswith("/") or "\\" in cleaned:
        return ""
    parts = [part for part in cleaned.split("/") if part not in ("", ".")]
    if any(part == ".." for part in parts):
        return ""
    return "/".join(parts)


# --------------------------------------------------------------------------
# Pages
# --------------------------------------------------------------------------


@bp.route("/")
@login_required
def index():
    """Workspace list page."""
    workspaces = Workspace.query.filter_by(user_id=current_user.id).order_by(
        Workspace.updated_at.desc()
    )
    return render_template("workspaces/index.html", workspaces=workspaces)


@bp.route("/<int:workspace_id>")
@login_required
def detail(workspace_id: int):
    """Workspace detail page showing its projects."""
    workspace = _get_workspace(workspace_id)
    return render_template("workspaces/detail.html", workspace=workspace)


@bp.route("/<int:workspace_id>/projects/<int:project_id>")
@login_required
def project_explorer(workspace_id: int, project_id: int):
    """Project explorer page (file tree, search, chat, analysis, stats)."""
    workspace = _get_workspace(workspace_id)
    project = Project.query.filter_by(
        id=project_id, workspace_id=workspace.id, user_id=current_user.id
    ).first_or_404()
    return render_template("workspaces/project.html", workspace=workspace, project=project)


# --------------------------------------------------------------------------
# API: workspaces
# --------------------------------------------------------------------------


@bp.route("/api/workspaces", methods=["GET"])
@login_required
def api_list_workspaces():
    workspaces = Workspace.query.filter_by(user_id=current_user.id).order_by(
        Workspace.updated_at.desc()
    )
    return jsonify([w.to_dict() for w in workspaces])


@bp.route("/api/workspaces", methods=["POST"])
@login_required
def api_create_workspace():
    data = request.get_json(silent=True) or {}
    name = (data.get("name") or "").strip()
    if not name:
        return jsonify({"error": "A workspace name is required."}), 400
    workspace = Workspace(
        user_id=current_user.id,
        name=name[:200],
        description=((data.get("description") or "").strip()[:2000] or None),
    )
    db.session.add(workspace)
    db.session.commit()
    return jsonify(workspace.to_dict()), 201


@bp.route("/api/workspaces/<int:workspace_id>", methods=["PATCH"])
@login_required
def api_update_workspace(workspace_id: int):
    workspace = _get_workspace(workspace_id)
    data = request.get_json(silent=True) or {}
    if "name" in data:
        name = (data.get("name") or "").strip()
        if not name:
            return jsonify({"error": "A workspace name is required."}), 400
        workspace.name = name[:200]
    if "description" in data:
        workspace.description = (data.get("description") or "").strip()[:2000] or None
    db.session.commit()
    return jsonify(workspace.to_dict())


@bp.route("/api/workspaces/<int:workspace_id>", methods=["DELETE"])
@login_required
def api_delete_workspace(workspace_id: int):
    workspace = _get_workspace(workspace_id)
    db.session.delete(workspace)
    db.session.commit()
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# API: workspace members
# Listing is member-scoped (any member can see the team); add/update/remove
# remain owner-only. Removal is a soft-delete that preserves history (135).
# --------------------------------------------------------------------------


@bp.route("/api/workspaces/<int:workspace_id>/members", methods=["GET"])
@login_required
def api_list_members(workspace_id: int):
    workspace = resolve_workspace(workspace_id)
    members = (
        WorkspaceMember.query.filter_by(workspace_id=workspace.id, status=STATUS_ACTIVE)
        .order_by(WorkspaceMember.joined_at)
        .all()
    )
    return jsonify([m.to_dict() for m in members])


@bp.route("/api/workspaces/<int:workspace_id>/members", methods=["POST"])
@login_required
def api_add_member(workspace_id: int):
    _get_workspace(workspace_id)
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    role = (data.get("role") or ROLE_VIEWER).strip().lower()
    if not username:
        return jsonify({"error": "A username is required."}), 400
    if role not in MEMBER_ROLES:
        return jsonify({"error": "Invalid role."}), 400
    user = User.query.filter_by(username=username).first()
    if user is None:
        return jsonify({"error": "No user with that username exists."}), 404
    if user.id == current_user.id:
        return jsonify({"error": "The owner is already a member."}), 400

    existing = WorkspaceMember.query.filter_by(workspace_id=workspace_id, user_id=user.id).first()
    if existing is not None and existing.status == STATUS_ACTIVE:
        return jsonify({"error": "That user is already a member."}), 409
    if existing is not None:
        # Reactivating a previously removed member preserves the row and the
        # unique (workspace_id, user_id) constraint.
        existing.reactivate()
        existing.role = role
        membership = existing
        metadata = {"role": role, "reactivated": True}
    else:
        membership = WorkspaceMember(workspace_id=workspace_id, user_id=user.id, role=role)
        db.session.add(membership)
        metadata = {"role": role}

    record_activity(
        workspace_id,
        EVENT_MEMBER_ADDED,
        actor=current_user,
        target=membership,
        metadata=metadata,
    )
    db.session.flush()
    notify(
        user,
        "membership",
        actor=current_user,
        workspace=db.session.get(Workspace, workspace_id),
        payload={"title": "You were added to a workspace", "role": role},
        link=url_for("collaboration.members_page", workspace_id=workspace_id),
    )
    db.session.commit()
    emit_event(
        "workspace.member_added",
        data={"user_id": user.id, "workspace_id": workspace_id, "role": role},
        workspace_id=workspace_id,
        user_id=current_user.id,
    )
    return jsonify(membership.to_dict()), 201


@bp.route("/api/workspaces/<int:workspace_id>/members/<int:user_id>", methods=["PATCH"])
@login_required
def api_update_member(workspace_id: int, user_id: int):
    _get_workspace(workspace_id)
    membership = _get_membership(workspace_id, user_id)
    data = request.get_json(silent=True) or {}
    role = (data.get("role") or "").strip().lower()
    if role not in MEMBER_ROLES:
        return jsonify({"error": "Invalid role."}), 400
    if role != membership.role:
        record_activity(
            workspace_id,
            EVENT_ROLE_CHANGED,
            actor=current_user,
            target=membership,
            metadata={"old_role": membership.role, "new_role": role},
        )
        notify(
            membership.user,
            "role_change",
            actor=current_user,
            workspace=db.session.get(Workspace, workspace_id),
            payload={
                "title": "Your workspace role changed",
                "old_role": membership.role,
                "new_role": role,
            },
            link=url_for("collaboration.members_page", workspace_id=workspace_id),
        )
    membership.role = role
    db.session.commit()
    return jsonify(membership.to_dict())


@bp.route("/api/workspaces/<int:workspace_id>/members/<int:user_id>", methods=["DELETE"])
@login_required
def api_remove_member(workspace_id: int, user_id: int):
    _get_workspace(workspace_id)
    membership = _get_membership(workspace_id, user_id)
    if membership.status != STATUS_ACTIVE:
        return jsonify({"error": "That member is already removed."}), 409
    membership.mark_removed()
    cancel_pending_for_user(workspace_id, user_id, current_user)
    record_activity(
        workspace_id,
        EVENT_MEMBER_REMOVED,
        actor=current_user,
        target=membership,
        metadata={"role": membership.role},
    )
    notify(
        membership.user,
        "membership",
        actor=current_user,
        workspace=db.session.get(Workspace, workspace_id),
        payload={"title": "You were removed from a workspace", "role": membership.role},
    )
    db.session.commit()
    emit_event(
        "workspace.member_removed",
        data={"user_id": user_id, "workspace_id": workspace_id},
        workspace_id=workspace_id,
        user_id=current_user.id,
    )
    return jsonify({"ok": True})


@bp.route("/api/workspaces/<int:workspace_id>/projects", methods=["GET"])
@login_required
def api_list_projects(workspace_id: int):
    workspace = _get_workspace(workspace_id)
    projects = Project.query.filter_by(workspace_id=workspace.id).order_by(
        Project.updated_at.desc()
    )
    return jsonify([p.to_dict() for p in projects])


# --------------------------------------------------------------------------
# API: project import
# --------------------------------------------------------------------------


@bp.route("/api/workspaces/<int:workspace_id>/projects", methods=["POST"])
@login_required
def api_import_project(workspace_id: int):
    """Import a project from an uploaded archive or a GitHub repository."""
    workspace = _get_workspace(workspace_id)
    if request.files.get("file"):
        return _import_archive(workspace)
    return _import_github(workspace)


def _import_archive(workspace: Workspace):
    uploaded = request.files.get("file")
    if uploaded is None or not uploaded.filename:
        return jsonify({"error": "No file was uploaded."}), 400

    name = Path(uploaded.filename).stem.strip() or "Untitled project"
    project = Project(
        workspace_id=workspace.id,
        user_id=current_user.id,
        name=name[:200],
        source=SOURCE_ARCHIVE,
    )
    db.session.add(project)
    db.session.commit()

    try:
        rows = extract_archive(uploaded.stream, uploaded.filename)
    except ProjectImportError as exc:
        db.session.delete(project)
        db.session.commit()
        return jsonify({"error": str(exc)}), 400

    if not rows:
        db.session.delete(project)
        db.session.commit()
        return jsonify({"error": "The archive contained no importable files."}), 400

    from app.services.importing import store_project_files

    store_project_files(project, rows)
    record_activity(
        workspace.id,
        EVENT_PROJECT_IMPORTED,
        actor=current_user,
        target=project,
        metadata={"source": SOURCE_ARCHIVE, "file_count": project.file_count},
    )
    db.session.commit()
    emit_event(
        "project.created",
        data={"project_id": project.id, "name": project.name, "source": SOURCE_ARCHIVE},
        workspace_id=workspace.id,
        user_id=current_user.id,
    )
    return jsonify(project.to_dict()), 201


def _import_github(workspace: Workspace):
    data = request.get_json(silent=True) or {}
    repo = (data.get("repo") or "").strip()
    if not repo:
        return jsonify({"error": "A repository in the form owner/name is required."}), 400
    try:
        full_name = validate_full_name(repo)
    except GitHubInvalidError as exc:
        return jsonify({"error": str(exc)}), 400

    project = Project(
        workspace_id=workspace.id,
        user_id=current_user.id,
        name=full_name.split("/")[1][:200],
        source=SOURCE_GITHUB,
        source_url=full_name,
    )
    db.session.add(project)
    db.session.commit()

    try:
        client = get_github_client()
        import_github_repo(project, full_name, client)
    except (GitHubError, ProjectImportError) as exc:
        db.session.delete(project)
        db.session.commit()
        return jsonify({"error": str(exc)}), 502

    record_activity(
        workspace.id,
        EVENT_PROJECT_IMPORTED,
        actor=current_user,
        target=project,
        metadata={"source": SOURCE_GITHUB, "file_count": project.file_count},
    )
    db.session.commit()
    emit_event(
        "project.created",
        data={"project_id": project.id, "name": project.name, "source": SOURCE_GITHUB},
        workspace_id=workspace.id,
        user_id=current_user.id,
    )
    return jsonify(project.to_dict()), 201


@bp.route("/api/projects/<int:project_id>", methods=["DELETE"])
@login_required
def api_delete_project(project_id: int):
    project = _get_project(project_id)
    workspace_id = project.workspace_id
    db.session.delete(project)
    db.session.commit()
    emit_event(
        "project.deleted",
        data={"project_id": project_id},
        workspace_id=workspace_id,
        user_id=current_user.id,
    )
    return jsonify({"ok": True})


# --------------------------------------------------------------------------
# API: file explorer
# --------------------------------------------------------------------------


@bp.route("/api/projects/<int:project_id>/tree", methods=["GET"])
@login_required
def api_project_tree(project_id: int):
    """Return the direct children of a directory (lazy file tree)."""
    project = _get_project(project_id)
    base = _validate_project_path(request.args.get("path", ""))
    if request.args.get("path") and not base and (request.args.get("path") or "").strip():
        return jsonify({"error": "Invalid path."}), 400

    dirs: set[str] = set()
    files = []
    prefix = f"{base}/" if base else ""
    for file in project.files:
        path = file.path
        if prefix and not path.startswith(prefix):
            continue
        rest = path[len(prefix) :] if prefix else path
        if "/" in rest:
            dirs.add(rest.split("/", 1)[0])
        else:
            files.append(file)

    sorted_dirs = sorted(dirs)
    sorted_files = sorted(files, key=lambda f: f.path)
    return jsonify(
        {
            "path": base,
            "directories": sorted_dirs[:MAX_TREE_ENTRIES],
            "files": [f.to_dict() for f in sorted_files[:MAX_TREE_ENTRIES]],
            "truncated": len(sorted_dirs) + len(sorted_files) > MAX_TREE_ENTRIES,
        }
    )


@bp.route("/api/projects/<int:project_id>/file", methods=["GET"])
@login_required
def api_project_file(project_id: int):
    """Return a single file's contents (None for binary/oversized files)."""
    project = _get_project(project_id)
    path = _validate_project_path(request.args.get("path", ""))
    if not path:
        return jsonify({"error": "A valid file path is required."}), 400
    file = next((f for f in project.files if f.path == path), None)
    if file is None:
        return jsonify({"error": "File not found in this project."}), 404
    return jsonify(
        {
            "path": file.path,
            "size": file.size,
            "language": file.language,
            "is_binary": file.is_binary,
            "searchable": file.content is not None,
            "content": file.content,
        }
    )


# --------------------------------------------------------------------------
# API: search
# --------------------------------------------------------------------------


@bp.route("/api/projects/<int:project_id>/search", methods=["GET"])
@login_required
def api_project_search(project_id: int):
    project = _get_project(project_id)
    if project.status != STATUS_READY:
        return jsonify({"error": "This project has not finished indexing."}), 409
    query = request.args.get("q", "").strip()
    if not query:
        return jsonify({"error": "A search query is required."}), 400
    case_sensitive = request.args.get("case", "0") == "1"
    limit = request.args.get("limit", type=int)
    result = search_project(project.id, query, case_sensitive=case_sensitive, limit=limit)
    return jsonify(result)


# --------------------------------------------------------------------------
# API: health dashboard
# --------------------------------------------------------------------------


@bp.route("/api/projects/<int:project_id>/stats", methods=["GET"])
@login_required
def api_project_stats(project_id: int):
    project = _get_project(project_id)
    files = list(project.files)

    languages = Counter(f.language or "Other" for f in files if f.language)
    searchable = sum(1 for f in files if f.content is not None)
    test_files = [f for f in files if _is_test_path(f.path)]
    doc_files = [f for f in files if f.path.rsplit(".", 1)[-1].lower() in ("md", "rst", "txt")]
    inventory = project_analysis.dependency_inventory(project)

    duration = None
    if project.indexed_at and project.created_at:
        duration = max(int((project.indexed_at - project.created_at).total_seconds()), 0)

    return jsonify(
        {
            "project": project.to_dict(),
            "file_count": project.file_count,
            "total_size_bytes": project.total_size_bytes,
            "searchable_file_count": searchable,
            "languages": languages.most_common(10),
            "test_file_count": len(test_files),
            "doc_file_count": len(doc_files),
            "dependency_count": len(inventory),
            "manifest_files": sorted({item["file"] for item in inventory}),
            "index_duration_seconds": duration,
        }
    )


def _is_test_path(path: str) -> bool:
    name = path.rsplit("/", 1)[-1].lower()
    return (
        name.startswith("test_")
        or name.startswith("tests_")
        or name.endswith("_test.py")
        or "tests/" in f"/{path}/"
        or "/test/" in f"/{path}/"
    )


# --------------------------------------------------------------------------
# API: AI project chat
# --------------------------------------------------------------------------


@bp.route("/api/projects/<int:project_id>/messages", methods=["GET"])
@login_required
def api_project_messages(project_id: int):
    project = _get_project(project_id)
    messages = ProjectMessage.query.filter_by(project_id=project.id).order_by(
        ProjectMessage.created_at
    )
    return jsonify([m.to_dict() for m in messages])


@bp.route("/api/projects/<int:project_id>/chat", methods=["POST"])
@login_required
def api_project_chat(project_id: int):
    """Answer a question about the project (non-streaming)."""
    project = _get_project(project_id)
    if project.status != STATUS_READY:
        return jsonify({"error": "This project has not finished indexing."}), 409
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "A message is required."}), 400

    db.session.add(ProjectMessage(project_id=project.id, role="user", content=content))
    result = project_analysis.chat_with_project(project, content)
    message = ProjectMessage(project_id=project.id, role="assistant", content=result["analysis"])
    db.session.add(message)
    db.session.commit()
    return (
        jsonify(
            {
                "assistant_message": message.to_dict(),
                "context_paths": result["context_paths"],
            }
        ),
        201,
    )


@bp.route("/api/projects/<int:project_id>/chat/stream", methods=["POST"])
@login_required
def api_project_chat_stream(project_id: int):
    """Stream an assistant reply about the project using Server-Sent Events."""
    project = _get_project(project_id)
    if project.status != STATUS_READY:
        return jsonify({"error": "This project has not finished indexing."}), 409
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "A message is required."}), 400

    history = list(project.messages)
    db.session.add(ProjectMessage(project_id=project.id, role="user", content=content))
    db.session.commit()
    messages = project_analysis.build_messages(project, content, history)

    def generate():
        try:
            provider = get_provider()
            for chunk in provider.stream(messages):
                yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"
        except LLMProviderError as exc:
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"
            return

        try:
            reply = project_analysis.chat_with_project(project, content)["analysis"]
        except LLMProviderError as exc:
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"
            return

        message = ProjectMessage(project_id=project.id, role="assistant", content=reply)
        db.session.add(message)
        db.session.commit()
        yield f"data: {json.dumps({'type': 'done', 'message': message.to_dict()})}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


# --------------------------------------------------------------------------
# API: project analysis
# --------------------------------------------------------------------------


@bp.route("/api/projects/<int:project_id>/analyze", methods=["POST"])
@login_required
def api_project_analyze(project_id: int):
    """Run a bounded analysis of the project (architecture/bugs/refactor/...)."""
    project = _get_project(project_id)
    if project.status != STATUS_READY:
        return jsonify({"error": "This project has not finished indexing."}), 409
    data = request.get_json(silent=True) or {}
    kind = (data.get("kind") or "architecture").strip().lower()
    if kind not in project_analysis.ANALYSIS_KINDS:
        return jsonify({"error": "Unsupported analysis kind."}), 400
    result = project_analysis.analyze_project(project, kind)
    record_activity(
        project.workspace_id,
        EVENT_AI_ANALYSIS_RUN,
        actor=current_user,
        target=project,
        metadata={"kind": result["kind"], "project_id": project.id, "project": project.name},
    )
    notify(
        current_user,
        "ai_event",
        actor=current_user,
        workspace=db.session.get(Workspace, project.workspace_id),
        project=project,
        payload={"title": f"Analysis complete: {project.name}", "kind": result["kind"]},
        link=url_for(
            "workspaces.project_explorer",
            workspace_id=project.workspace_id,
            project_id=project.id,
        ),
    )
    db.session.commit()
    emit_event(
        "ai.analysis.completed",
        data={"project_id": project.id, "kind": result["kind"]},
        workspace_id=project.workspace_id,
        user_id=current_user.id,
    )
    if result["kind"] == "stellar":
        emit_event(
            "stellar.analysis.completed",
            data={
                "project_id": project.id,
                "detected": bool(result.get("detected")),
                "confidence": result.get("confidence"),
            },
            workspace_id=project.workspace_id,
            user_id=current_user.id,
        )
    return jsonify(result)
