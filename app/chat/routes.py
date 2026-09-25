"""Chat routes: UI page, conversation CRUD, and SSE streaming."""

import hashlib
import json
import secrets
from datetime import UTC, datetime, timedelta

from flask import Response, abort, jsonify, render_template, request, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from app.chat import bp
from app.extensions import db
from app.models import Conversation, Message, ProjectFile, Workspace
from app.models.project import STATUS_READY
from app.models import ConversationShare
from app.services.llm import LLMProviderError, get_provider

#: Cap on files returned per project in the chat file tree (keeps the payload
#: bounded for large imports); ``truncated`` signals the client when it applies.
MAX_TREE_FILES = 500


def _get_conversation(conversation_id: int) -> Conversation:
    """Return the current user's conversation or abort with 404."""
    conversation = Conversation.query.filter_by(
        id=conversation_id, user_id=current_user.id
    ).first_or_404()
    return conversation


def _share_from_token(token: str) -> ConversationShare:
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()
    share = ConversationShare.query.filter_by(token_hash=digest).first()
    if share is None or share.is_expired():
        if share is not None:
            db.session.delete(share)
            db.session.commit()
        abort(404)
    return share


def _share_payload(share: ConversationShare, token: str | None = None) -> dict:
    payload = {
        "id": share.id,
        "permission": share.permission,
        "expires_at": share.expires_at.isoformat(),
        "created_at": share.created_at.isoformat(),
    }
    if token:
        payload["url"] = url_for("chat.shared_conversation_page", token=token, _external=True)
    return payload


@bp.route("/")
@login_required
def index():
    """Render the chat interface with the user's conversations."""
    conversations = (
        Conversation.query.filter_by(user_id=current_user.id)
        .order_by(Conversation.is_pinned.desc(), Conversation.updated_at.desc())
        .all()
    )
    return render_template("chat/index.html", conversations=conversations)


@bp.route("/conversations", methods=["GET"])
@login_required
def list_conversations():
    """Return the current user's conversations as JSON (for search/refresh)."""
    query = request.args.get("q", "").strip().lower()
    base = Conversation.query.filter_by(user_id=current_user.id)
    if query:
        base = base.filter(func.lower(Conversation.title).contains(query))
    conversations = base.order_by(Conversation.updated_at.desc()).all()
    return jsonify([c.to_dict() for c in conversations])


@bp.route("/conversations", methods=["POST"])
@login_required
def create_conversation():
    """Create a new empty conversation."""
    data = request.get_json(silent=True) or {}
    title = (data.get("title") or "New conversation").strip()[:200]
    conversation = Conversation(user_id=current_user.id, title=title or "New conversation")
    db.session.add(conversation)
    db.session.commit()
    return jsonify(conversation.to_dict()), 201


@bp.route("/conversations/<int:conversation_id>", methods=["GET"])
@login_required
def get_conversation(conversation_id: int):
    """Return a conversation with its full message history."""
    conversation = _get_conversation(conversation_id)
    payload = conversation.to_dict()
    payload["messages"] = [m.to_dict() for m in conversation.messages]
    return jsonify(payload)


@bp.route("/conversations/<int:conversation_id>", methods=["PATCH"])
@login_required
def update_conversation(conversation_id: int):
    """Rename or pin/unpin a conversation."""
    conversation = _get_conversation(conversation_id)
    data = request.get_json(silent=True) or {}
    if "title" in data:
        conversation.title = (data.get("title") or "Untitled").strip()[:200]
    if "is_pinned" in data:
        conversation.is_pinned = bool(data["is_pinned"])
    db.session.commit()
    return jsonify(conversation.to_dict())


@bp.route("/conversations/<int:conversation_id>", methods=["DELETE"])
@login_required
def delete_conversation(conversation_id: int):
    """Delete a conversation and all of its messages."""
    conversation = _get_conversation(conversation_id)
    db.session.delete(conversation)
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/conversations/<int:conversation_id>/export")
@login_required
def export_conversation(conversation_id: int):
    """Export a conversation as a downloadable JSON document."""
    conversation = _get_conversation(conversation_id)
    payload = {
        "conversation": conversation.to_dict(),
        "messages": [m.to_dict() for m in conversation.messages],
    }
    filename = f"conversation-{conversation.id}.json"
    return Response(
        json.dumps(payload, indent=2),
        mimetype="application/json",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@bp.route("/conversations/<int:conversation_id>/shares", methods=["GET", "POST"])
@login_required
def conversation_shares(conversation_id: int):
    """List or create secure, expiring links owned by the current user."""
    conversation = _get_conversation(conversation_id)
    if request.method == "GET":
        return jsonify(
            [_share_payload(share) for share in conversation.shares if not share.is_expired()]
        )

    data = request.get_json(silent=True) or {}
    permission = data.get("permission", "read_only")
    if permission not in {"read_only", "commenter"}:
        return jsonify({"error": "Permission must be read_only or commenter."}), 400
    try:
        hours = min(max(int(data.get("expires_hours", 168)), 1), 720)
    except (TypeError, ValueError):
        return jsonify({"error": "expires_hours must be an integer."}), 400

    token = secrets.token_urlsafe(32)
    share = ConversationShare(
        conversation_id=conversation.id,
        created_by=current_user.id,
        token_hash=hashlib.sha256(token.encode("utf-8")).hexdigest(),
        expires_at=datetime.now(UTC) + timedelta(hours=hours),
        permission=permission,
    )
    db.session.add(share)
    db.session.commit()
    return jsonify(_share_payload(share, token)), 201


@bp.route("/conversations/<int:conversation_id>/shares/<int:share_id>", methods=["DELETE"])
@login_required
def revoke_conversation_share(conversation_id: int, share_id: int):
    conversation = _get_conversation(conversation_id)
    share = ConversationShare.query.filter_by(
        id=share_id, conversation_id=conversation.id
    ).first_or_404()
    db.session.delete(share)
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/shared/<token>")
def shared_conversation_page(token: str):
    """Render a token-scoped read-only conversation page."""
    _share_from_token(token)
    return render_template("chat/shared.html", token=token)


@bp.route("/api/shared/<token>")
def shared_conversation(token: str):
    share = _share_from_token(token)
    conversation = db.session.get(Conversation, share.conversation_id)
    return jsonify(
        {
            "conversation": conversation.to_dict(),
            "messages": [message.to_dict() for message in conversation.messages],
            "permission": share.permission,
            "expires_at": share.expires_at.isoformat(),
        }
    )


@bp.route("/api/shared/<token>/messages", methods=["POST"])
def add_shared_message(token: str):
    """Allow commenter links to append a message without exposing the owner account."""
    share = _share_from_token(token)
    if share.permission != "commenter":
        return jsonify({"error": "This share is read-only."}), 403
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content or len(content) > 20_000:
        return jsonify(
            {"error": "Message content is required and must be under 20,000 characters."}
        ), 400
    message = Message(role="user", content=content, conversation_id=share.conversation_id)
    db.session.add(message)
    db.session.commit()
    return jsonify(message.to_dict()), 201


@bp.route("/conversations/<int:conversation_id>/messages", methods=["POST"])
@login_required
def send_message(conversation_id: int):
    """Store a user message and return the full (non-streamed) assistant reply.

    Used by clients that do not support SSE; the chat UI uses the streaming
    endpoint below instead.
    """
    conversation = _get_conversation(conversation_id)
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Message content is required."}), 400

    history = [{"role": m.role, "content": m.content} for m in conversation.messages]
    conversation.messages.append(Message(role="user", content=content))

    try:
        provider = get_provider()
        reply = provider.complete([*history, {"role": "user", "content": content}])
    except LLMProviderError as exc:
        db.session.rollback()
        return jsonify({"error": str(exc)}), 502

    conversation.messages.append(Message(role="assistant", content=reply))
    if conversation.title == "New conversation":
        conversation.title = content.strip()[:60] or "New conversation"
    db.session.commit()
    return jsonify({"assistant_message": conversation.messages[-1].to_dict()}), 201


@bp.route("/conversations/<int:conversation_id>/stream", methods=["POST"])
@login_required
def stream_message(conversation_id: int):
    """Stream an assistant reply using Server-Sent Events.

    The user message is persisted immediately, then assistant tokens are
    streamed as ``data:`` events. A final ``done`` event carries the persisted
    assistant message so the client can keep its UI in sync with the database.
    """
    conversation = _get_conversation(conversation_id)
    data = request.get_json(silent=True) or {}
    content = (data.get("content") or "").strip()
    if not content:
        return jsonify({"error": "Message content is required."}), 400

    history = [{"role": m.role, "content": m.content} for m in conversation.messages]
    conversation.messages.append(Message(role="user", content=content))
    db.session.commit()

    def generate():
        try:
            provider = get_provider()
            for chunk in provider.stream([*history, {"role": "user", "content": content}]):
                yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"
        except LLMProviderError as exc:
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"
            return

        # Persist the complete reply built from the streamed chunks is not
        # possible inside the generator without buffering; instead the mock
        # provider's complete() is used for a canonical response.
        try:
            provider = get_provider()
            reply = provider.complete([*history, {"role": "user", "content": content}])
        except LLMProviderError as exc:
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"
            return

        message = Message(role="assistant", content=reply)
        conversation.messages.append(message)
        db.session.commit()
        yield f"data: {json.dumps({'type': 'done', 'message': message.to_dict()})}\n\n"

    return Response(generate(), mimetype="text/event-stream")


# --------------------------------------------------------------------------
# API: project file tree (for the chat sidebar)
# --------------------------------------------------------------------------


@bp.route("/api/project-files")
@login_required
def api_project_files():
    """Return the current user's workspaces/projects/files for the chat tree.

    Grouped by workspace, then project, so the chat sidebar can render a
    collapsible tree. File content is fetched separately (owner-scoped) via the
    workspace file endpoint when a file is opened.
    """
    workspaces = (
        Workspace.query.filter_by(user_id=current_user.id).order_by(Workspace.name.asc()).all()
    )

    result = []
    for workspace in workspaces:
        projects = []
        for project in workspace.projects:
            if project.status != STATUS_READY:
                projects.append(
                    {
                        "id": project.id,
                        "name": project.name,
                        "status": project.status,
                        "truncated": False,
                        "files": [],
                    }
                )
                continue

            rows = (
                ProjectFile.query.filter_by(project_id=project.id)
                .order_by(ProjectFile.path.asc())
                .limit(MAX_TREE_FILES + 1)
                .all()
            )
            truncated = len(rows) > MAX_TREE_FILES
            files = []
            for file in rows[:MAX_TREE_FILES]:
                payload = file.to_dict()
                payload["project_id"] = project.id
                payload["created_at"] = file.created_at.isoformat() if file.created_at else None
                files.append(payload)

            projects.append(
                {
                    "id": project.id,
                    "name": project.name,
                    "status": project.status,
                    "truncated": truncated,
                    "files": files,
                }
            )

        result.append({"id": workspace.id, "name": workspace.name, "projects": projects})

    return jsonify(result)
