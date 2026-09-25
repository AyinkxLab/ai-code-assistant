"""Chat routes: UI page, conversation CRUD, sharing, and SSE streaming."""

import json

from flask import Response, abort, jsonify, render_template, request, stream_with_context, url_for
from flask_login import current_user, login_required
from sqlalchemy import func

from app.chat import bp
from app.extensions import db
from app.models import Conversation, ConversationShare, Message, ProjectFile, User, Workspace
from app.models.project import STATUS_READY
from app.services.llm import LLMProviderError
from app.services.notifications import notify
from app.services.provider_config import (
    DEFAULT_TEMPERATURE,
    ProviderSettingsError,
    apply_settings,
    build_provider,
    provider_options,
)
from app.services.providers.registry import resolve_provider_name
from app.services.providers.retry import RetryingProvider


def _get_conversation(conversation_id: int) -> Conversation:
    """Return the current user's conversation or abort with 404."""
    conversation = Conversation.query.filter_by(
        id=conversation_id, user_id=current_user.id
    ).first_or_404()
    return conversation


def _get_visible_conversation(conversation_id: int) -> Conversation:
    """Return a conversation readable by the current user (owner or shared)."""
    conversation = Conversation.query.filter_by(id=conversation_id).first_or_404()
    if conversation.user_id == current_user.id or conversation.is_shared_with(current_user.id):
        return conversation
    abort(404)


def _shared_conversation_ids() -> list[int]:
    """Conversation ids the current user can read because they were shared in."""
    rows = (
        ConversationShare.query.filter_by(user_id=current_user.id)
        .with_entities(ConversationShare.conversation_id)
        .all()
    )
    return [row[0] for row in rows]


def _conversation_messages(history: list[dict], content: str, conversation) -> list[dict]:
    """Build the outgoing message list, honoring the conversation's system prompt."""
    messages = [*history, {"role": "user", "content": content}]
    if conversation.system_prompt:
        messages.insert(0, {"role": "system", "content": conversation.system_prompt})
    return messages


def _generation_kwargs(conversation) -> dict:
    """Return the ``model``/``params`` passed to the provider for a conversation."""
    params = {}
    if conversation.temperature is not None:
        params["temperature"] = conversation.temperature
    return {"model": conversation.model, "params": params or None}
#: Cap on files returned per project in the chat file tree (keeps the payload
#: bounded for large imports); ``truncated`` signals the client when it applies.
MAX_TREE_FILES = 500


@bp.route("/")
@login_required
def index():
    """Render the chat interface with the user's conversations."""
    shared_ids = _shared_conversation_ids()
    conversations = (
        Conversation.query.filter(
            db.or_(Conversation.user_id == current_user.id, Conversation.id.in_(shared_ids))
        )
        .order_by(Conversation.is_pinned.desc(), Conversation.updated_at.desc())
        .all()
    )
    return render_template("chat/index.html", conversations=conversations)


@bp.route("/conversations", methods=["GET"])
@login_required
def list_conversations():
    """Return the current user's conversations as JSON (for search/refresh)."""
    query = request.args.get("q", "").strip().lower()
    shared_ids = _shared_conversation_ids()
    base = Conversation.query.filter(
        db.or_(Conversation.user_id == current_user.id, Conversation.id.in_(shared_ids))
    )
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
    try:
        apply_settings(conversation, data, current_user)
    except ProviderSettingsError as exc:
        return jsonify({"error": str(exc)}), 400
    db.session.add(conversation)
    db.session.commit()
    return jsonify(conversation.to_dict()), 201


@bp.route("/conversations/<int:conversation_id>", methods=["GET"])
@login_required
def get_conversation(conversation_id: int):
    """Return a conversation with its full message history.

    Readable by the owner and by any user the conversation was shared with.
    """
    conversation = _get_visible_conversation(conversation_id)
    payload = conversation.to_dict()
    payload["messages"] = [m.to_dict() for m in conversation.messages]
    payload["shared_user_ids"] = [s.user_id for s in conversation.shares]
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


@bp.route("/conversations/<int:conversation_id>/settings", methods=["PATCH"])
@login_required
def update_conversation_settings(conversation_id: int):
    """Persist the conversation's provider/model/temperature/system prompt.

    Settings apply to subsequent messages, so a new conversation is not needed
    to change them.
    """
    conversation = _get_conversation(conversation_id)
    data = request.get_json(silent=True) or {}
    try:
        apply_settings(conversation, data, current_user)
    except ProviderSettingsError as exc:
        return jsonify({"error": str(exc)}), 400
    db.session.commit()
    return jsonify(conversation.to_dict())


@bp.route("/api/options")
@login_required
def api_options():
    """Return the providers/models the current user can select (issue #12)."""
    return jsonify(
        {
            "providers": provider_options(current_user),
            "default_provider": resolve_provider_name(),
            "default_temperature": DEFAULT_TEMPERATURE,
        }
    )


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


@bp.route("/conversations/<int:conversation_id>/shares", methods=["GET"])
@login_required
def list_shares(conversation_id: int):
    """Return who a conversation was shared with (owner only)."""
    conversation = _get_conversation(conversation_id)
    return jsonify([s.to_dict() for s in conversation.shares])


@bp.route("/conversations/<int:conversation_id>/shares", methods=["POST"])
@login_required
def share_conversation(conversation_id: int):
    """Share a conversation with another registered user (owner only).

    Creates a persistent ``ConversationShare`` row and a ``share``
    notification for the recipient so the share is discoverable in the app.
    """
    conversation = _get_conversation(conversation_id)
    data = request.get_json(silent=True) or {}
    username = (data.get("username") or "").strip()
    if not username:
        return jsonify({"error": "A recipient username is required."}), 400

    recipient = User.query.filter_by(username=username).first()
    if recipient is None:
        return jsonify({"error": "No user with that username exists."}), 404
    if recipient.id == current_user.id:
        return jsonify({"error": "You cannot share a conversation with yourself."}), 400
    existing = ConversationShare.query.filter_by(
        conversation_id=conversation.id, user_id=recipient.id
    ).first()
    if existing is not None:
        return jsonify({"error": "This conversation is already shared with that user."}), 409

    share = ConversationShare(
        conversation_id=conversation.id,
        user_id=recipient.id,
        shared_by_id=current_user.id,
    )
    db.session.add(share)
    notify(
        recipient,
        "share",
        actor=current_user,
        payload={
            "title": conversation.title,
            "conversation_id": conversation.id,
        },
        link=url_for("chat.index", conversation=conversation.id),
    )
    db.session.commit()
    return jsonify(share.to_dict()), 201


@bp.route("/conversations/<int:conversation_id>/shares/<int:user_id>", methods=["DELETE"])
@login_required
def unshare_conversation(conversation_id: int, user_id: int):
    """Remove a previously created share (owner only)."""
    conversation = _get_conversation(conversation_id)
    share = ConversationShare.query.filter_by(
        conversation_id=conversation.id, user_id=user_id
    ).first_or_404()
    db.session.delete(share)
    db.session.commit()
    return jsonify({"ok": True})


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
    messages = _conversation_messages(history, content, conversation)

    try:
        provider = RetryingProvider(build_provider(current_user, conversation.provider))
        reply = provider.chat(messages, **_generation_kwargs(conversation)).content
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
    messages = _conversation_messages(history, content, conversation)
    generation = _generation_kwargs(conversation)

    def persist_assistant(reply: str):
        """Persist an assistant message, or return ``None`` when empty.

        Used both for the completed reply and for the partial text kept when
        the client cancels mid-stream (issue #11).
        """
        reply = reply or ""
        if not reply.strip():
            return None
        message = Message(role="assistant", content=reply)
        conversation.messages.append(message)
        db.session.commit()
        return message

    def generate():
        # Accumulate chunks so a cancelled stream can still keep what it got.
        chunks: list[str] = []
        try:
            provider = RetryingProvider(build_provider(current_user, conversation.provider))
            for chunk in provider.stream(messages, **generation):
                chunks.append(chunk)
                yield f"data: {json.dumps({'type': 'token', 'content': chunk})}\n\n"
        except GeneratorExit:
            # The client disconnected (Stop button or closed tab). Persist the
            # partial reply so the user keeps what was generated, then stop.
            persist_assistant("".join(chunks))
            raise
        except LLMProviderError as exc:
            # A provider failure mid-stream: keep the partial text too.
            persist_assistant("".join(chunks))
            yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"
            return

        message = persist_assistant("".join(chunks))
        if message is None:
            # The provider streamed no text; fall back to a single completion.
            try:
                provider = RetryingProvider(build_provider(current_user, conversation.provider))
                message = persist_assistant(provider.chat(messages, **generation).content)
            except LLMProviderError as exc:
                yield f"data: {json.dumps({'type': 'error', 'error': str(exc)})}\n\n"
                return

        payload = {"type": "done", "message": message.to_dict() if message else None}
        yield f"data: {json.dumps(payload)}\n\n"

    return Response(stream_with_context(generate()), mimetype="text/event-stream")


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
