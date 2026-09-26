"""Chat JSON API (``/api`` namespace).

Machine-facing REST surface for the chat feature:

    POST   /api/conversations                      create a conversation
    GET    /api/conversations                      list the user's conversations
    GET    /api/conversations/<id>                 conversation with its messages
    POST   /api/conversations/<id>/messages        send a message (LLM reply)
    DELETE /api/conversations/<id>                 delete (cascade)

All routes require authentication and are owner-scoped. Errors use RFC 7807
(``application/problem+json``) documents so API clients never receive HTML error
pages, and every route is rate-limited per user. The ``/chat`` UI routes share
the same model/service layer.
"""

from __future__ import annotations

import functools

from flask import Blueprint, current_app, jsonify, request
from flask_login import current_user

from app.chat import routes as chat_routes
from app.extensions import db
from app.models import Conversation, Message
from app.services import ratelimit
from app.services.llm import LLMProviderError, provider_status
from app.services.provider_config import ProviderSettingsError, apply_settings, build_provider
from app.services.providers.retry import RetryingProvider

bp = Blueprint("chat_api", __name__, url_prefix="/api")


def _problem(status: int, title: str, detail: str | None = None, **extra):
    """Build an RFC 7807 ``application/problem+json`` response."""
    payload = {"type": "about:blank", "title": title, "status": status}
    if detail:
        payload["detail"] = detail
    payload.update(extra)
    response = jsonify(payload)
    response.status_code = status
    response.mimetype = "application/problem+json"
    return response


def _login_required(view):
    """Reject unauthenticated API calls with a 401 problem document."""

    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        if not current_user.is_authenticated:
            return _problem(401, "Authentication required.", "Log in to use this endpoint.")
        return view(*args, **kwargs)

    return wrapper


def _rate_limit(bucket: str):
    """Enforce the per-user chat rate limit, returning 429 as RFC 7807."""

    def decorator(view):
        @functools.wraps(view)
        def wrapper(*args, **kwargs):
            max_hits = current_app.config.get("RATE_LIMIT_CHAT_MAX", 30)
            window = current_app.config.get("RATE_LIMIT_CHAT_WINDOW", 60)
            allowed, retry_after = ratelimit.consume(
                f"api-chat:{bucket}:user:{current_user.get_id()}",
                max_hits=max_hits,
                window=window,
            )
            if not allowed:
                response = _problem(429, "Rate limit exceeded.", "Please retry later.")
                response.headers["Retry-After"] = str(retry_after)
                return response
            return view(*args, **kwargs)

        return wrapper

    return decorator


def _owned_conversation(conversation_id: int) -> Conversation | None:
    """Return the conversation only when it belongs to the current user."""
    return Conversation.query.filter_by(id=conversation_id, user_id=current_user.id).first()


def _json_object() -> dict | None:
    """Return the request body as a dict, or ``None`` when it is not one."""
    data = request.get_json(silent=True)
    if data is None:
        return {}
    return data if isinstance(data, dict) else None


@bp.route("/conversations", methods=["GET"])
@_login_required
@_rate_limit("list")
def list_conversations():
    """List the current user's conversations, newest first."""
    conversations = (
        Conversation.query.filter_by(user_id=current_user.id)
        .order_by(Conversation.updated_at.desc())
        .all()
    )
    return jsonify([conversation.to_dict() for conversation in conversations])


@bp.route("/conversations", methods=["POST"])
@_login_required
@_rate_limit("create")
def create_conversation():
    """Create a new conversation with an optional title."""
    data = _json_object()
    if data is None:
        return _problem(400, "Invalid request body.", "A JSON object body is required.")
    title = data.get("title")
    if title is not None and not isinstance(title, str):
        return _problem(400, "Invalid title.", "title must be a string.")
    conversation = Conversation(
        user_id=current_user.id,
        title=(title or "New conversation").strip()[:200] or "New conversation",
    )
    try:
        apply_settings(conversation, data, current_user)
    except ProviderSettingsError as exc:
        return _problem(400, "Invalid generation settings.", str(exc))
    db.session.add(conversation)
    db.session.commit()
    return jsonify(conversation.to_dict()), 201


@bp.route("/conversations/<int:conversation_id>", methods=["GET"])
@_login_required
@_rate_limit("get")
def get_conversation(conversation_id: int):
    """Return a conversation together with its message history."""
    conversation = _owned_conversation(conversation_id)
    if conversation is None:
        return _problem(404, "Conversation not found.", "No such conversation exists.")
    payload = conversation.to_dict()
    payload["messages"] = [message.to_dict() for message in conversation.messages]
    return jsonify(payload)


@bp.route("/conversations/<int:conversation_id>", methods=["DELETE"])
@_login_required
@_rate_limit("delete")
def delete_conversation(conversation_id: int):
    """Delete a conversation and its messages (cascade)."""
    conversation = _owned_conversation(conversation_id)
    if conversation is None:
        return _problem(404, "Conversation not found.", "No such conversation exists.")
    db.session.delete(conversation)
    db.session.commit()
    return jsonify({"ok": True})


@bp.route("/conversations/<int:conversation_id>/messages", methods=["POST"])
@_login_required
@_rate_limit("message")
def send_message(conversation_id: int):
    """Persist the user message and return the assistant's reply."""
    conversation = _owned_conversation(conversation_id)
    if conversation is None:
        return _problem(404, "Conversation not found.", "No such conversation exists.")
    data = _json_object()
    if data is None:
        return _problem(400, "Invalid request body.", "A JSON object body is required.")
    content = (data.get("content") or "").strip()
    attachment_ids = data.get("attachment_ids") or []
    if not content and not attachment_ids:
        return _problem(400, "Invalid message.", "Message content or an image is required.")
    if not content:
        content = "(image attached)"

    status = provider_status(current_user)
    if not status["configured"]:
        return _problem(
            503,
            "Provider not configured.",
            "No API key is configured for the selected provider.",
            code="provider_not_configured",
            provider=status.get("provider"),
        )

    user_message = Message(role="user", content=content)
    conversation.messages.append(user_message)
    error = chat_routes._link_attachments(conversation, user_message, attachment_ids)
    if error:
        db.session.rollback()
        return _problem(400, "Invalid attachment.", error)

    messages = chat_routes._conversation_messages(conversation)
    try:
        provider = RetryingProvider(build_provider(current_user, conversation.provider))
        reply = provider.chat(messages, **chat_routes._generation_kwargs(conversation)).content
    except LLMProviderError as exc:
        db.session.rollback()
        return _problem(502, "Provider error.", str(exc))

    conversation.messages.append(Message(role="assistant", content=reply))
    if conversation.title == "New conversation":
        conversation.title = content.strip()[:60] or "New conversation"
    db.session.commit()
    return jsonify({"assistant_message": conversation.messages[-1].to_dict()}), 201
