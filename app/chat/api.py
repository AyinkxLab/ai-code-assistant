"""Chat JSON API (``/api`` namespace).

Machine-facing REST surface for the chat feature:

    POST   /api/conversations                      create a conversation
    GET    /api/conversations                      list the user's conversations
    GET    /api/conversations/<id>                 conversation with its messages
    POST   /api/conversations/<id>/messages        send a message (LLM reply)
    DELETE /api/conversations/<id>                 delete (cascade)
    GET    /api/usage                              per-user token usage summary
    GET    /api/usage/breakdown                    usage and cost by source
    GET    /api/usage/export                       bounded JSON usage export
    GET    /api/usage/export.csv                   bounded, streaming CSV export
    GET    /api/usage/top                          top conversations/projects by cost

All routes require authentication and are owner-scoped. Errors use RFC 7807
(``application/problem+json``) documents so API clients never receive HTML error
pages, and every route is rate-limited per user. The ``/chat`` UI routes share
the same model/service layer.
"""

from __future__ import annotations

import functools
import json

from flask import Blueprint, Response, current_app, jsonify, request, stream_with_context
from flask_login import current_user
from sqlalchemy.orm import selectinload

from app.chat import routes as chat_routes
from app.collaboration import realtime
from app.extensions import db
from app.models import Conversation, Message
from app.services import budgets, ratelimit, streaming, usage_ledger
from app.services.currency import get_currency_config
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


def _sse_event(event: str, data: dict) -> str:
    """Encode a single Server-Sent Event frame."""
    return f"event: {event}\ndata: {json.dumps(data)}\n\n"


def _stream_reply(conversation: Conversation, reply: str):
    """Yield SSE frames for an assistant reply, ending with ``message_end``.

    The stream is chunked so the client can render a streaming cursor while
    the assistant is generating. A terminal ``message_end`` event always
    carries the persisted message so a dropped connection can be recovered.
    """
    yield _sse_event("message_start", {"conversation_id": conversation.id})
    for chunk in streaming.chunk_text(reply):
        yield _sse_event("message_delta", {"delta": chunk})
    yield _sse_event(
        "message_end",
        {"assistant_message": conversation.messages[-1].to_dict()},
    )


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


def _usage_totals(conversation: Conversation) -> dict:
    """Sum token usage across the conversation's assistant messages."""
    prompt = completion = total = 0
    for message in conversation.messages:
        if message.total_tokens is None:
            continue
        prompt += message.prompt_tokens or 0
        completion += message.completion_tokens or 0
        total += message.total_tokens or 0
    return {
        "prompt_tokens": prompt,
        "completion_tokens": completion,
        "total_tokens": total,
    }


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
        .options(
            selectinload(Conversation.messages),
            selectinload(Conversation.shares),
        )
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
    payload["usage"] = _usage_totals(conversation)
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
@budgets.budget_guard
@ratelimit.ai_limit
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

    context_messages, context_error, context_status = chat_routes._github_context_messages(
        current_user, content
    )
    if context_error is not None:
        extras = {key: value for key, value in context_error.items() if key != "error"}
        return _problem(
            context_status,
            "GitHub context unavailable.",
            context_error["error"],
            **extras,
        )

    user_message = Message(role="user", content=content)
    conversation.messages.append(user_message)
    error = chat_routes._link_attachments(conversation, user_message, attachment_ids)
    if error:
        db.session.rollback()
        return _problem(400, "Invalid attachment.", error)
    # Persist the user turn before calling the provider so a reload cannot lose it.
    db.session.commit()
    realtime.broadcast_message(conversation, user_message, sender=current_user)

    messages = chat_routes._conversation_messages(conversation, context_messages)
    try:
        provider = RetryingProvider(build_provider(current_user, conversation.provider))
        completion = provider.chat(messages, **chat_routes._generation_kwargs(conversation))
    except LLMProviderError as exc:
        interrupted = Message(
            role="assistant",
            content=str(exc) or "The response was interrupted.",
            interrupted=True,
        )
        conversation.messages.append(interrupted)
        db.session.commit()
        return _problem(
            502,
            "Provider error.",
            str(exc),
            assistant_message=interrupted.to_dict(),
        )

    assistant_message = Message(role="assistant", content=completion.content)
    usage = getattr(completion, "usage", None)
    if usage is not None and usage.has_usage:
        assistant_message.prompt_tokens = usage.prompt_tokens or 0
        assistant_message.completion_tokens = usage.completion_tokens or 0
        assistant_message.total_tokens = usage.total_tokens or 0
    conversation.messages.append(assistant_message)
    provider_name, provider_model = usage_ledger.current_provider_metadata(user=current_user)
    usage_ledger.record_usage(
        source=usage_ledger.SOURCE_CHAT,
        prompt_tokens=assistant_message.prompt_tokens or 0,
        completion_tokens=assistant_message.completion_tokens or 0,
        total_tokens=assistant_message.total_tokens,
        tokens_estimated=not (usage is not None and usage.has_usage),
        user=current_user,
        conversation=conversation,
        provider=provider_name,
        model=provider_model,
        status=usage_ledger.STATUS_SUCCESS,
        commit=False,
    )
    if conversation.title == "New conversation":
        conversation.title = content.strip()[:60] or "New conversation"
    db.session.commit()
    realtime.broadcast_message(conversation, user_message, sender=current_user)
    realtime.broadcast_message(conversation, assistant_message)
    if request.accept_mimetypes.best == "text/event-stream":
        response = current_app.response_class(
            _stream_reply(conversation, completion.content),
            mimetype="text/event-stream",
        )
        response.headers["Cache-Control"] = "no-cache"
        response.headers["X-Accel-Buffering"] = "no"
        return response
    payload = {"assistant_message": conversation.messages[-1].to_dict()}
    payload["usage"] = _usage_totals(conversation)
    return jsonify(payload), 201


@bp.route("/usage", methods=["GET"])
@_login_required
@_rate_limit("usage")
def usage_summary():
    """Return the current user's total token consumption."""
    conversations = Conversation.query.filter_by(user_id=current_user.id).all()
    prompt = completion = total = 0
    for conversation in conversations:
        totals = _usage_totals(conversation)
        prompt += totals["prompt_tokens"]
        completion += totals["completion_tokens"]
        total += totals["total_tokens"]
    return jsonify(
        {
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
        }
    )


@bp.route("/usage/breakdown", methods=["GET"])
@_login_required
@_rate_limit("usage")
def usage_breakdown():
    """Return usage counts and estimated cost grouped by source, provider and model.

    The authenticated user's records are always the outer scope, so an optional
    ``workspace_id`` or ``project_id`` filter can never expose another account's
    usage. Unknown historical sources/providers/models are grouped under
    ``unknown`` rather than dropped or causing a 500. Provider/model groups are
    bounded by ``limit`` (1-100, default 20); overflow is merged into ``other``
    and flagged with ``*_truncated``.
    """
    workspace_id = request.args.get("workspace_id", type=int)
    project_id = request.args.get("project_id", type=int)
    limit = request.args.get("limit", type=int)
    if limit is None:
        limit = usage_ledger.BREAKDOWN_DEFAULT_LIMIT
    limit = max(1, min(limit, usage_ledger.BREAKDOWN_MAX_LIMIT))
    payload = usage_ledger.breakdown_payload(
        user_id=current_user.id,
        workspace_id=workspace_id,
        project_id=project_id,
        limit=limit,
    )
    payload["currency"] = get_currency_config()
    payload["cost_is_estimate"] = True
    return jsonify(payload)


@bp.route("/usage/export.csv", methods=["GET"])
@_login_required
@_rate_limit("usage")
def usage_export_csv():
    """Export the current user's usage ledger as bounded CSV for billing.

    The authenticated user is always the outer scope; ``workspace_id``,
    ``project_id``, and ``source`` only narrow that user's own rows. Rows are
    serialized with the standard CSV writer so commas, quotes, and newlines in
    provider/model labels round-trip correctly.

    The row cap defaults to :data:`usage_ledger.EXPORT_CSV_MAX_ROWS` (5,000) and
    can only be lowered by ``USAGE_EXPORT_CSV_MAX_ROWS``. When rows are omitted,
    the response sets ``X-Export-Truncated: true`` and never implies a complete
    export. The body is streamed from pre-serialized line chunks.
    """
    configured = current_app.config.get(
        "USAGE_EXPORT_CSV_MAX_ROWS", usage_ledger.EXPORT_CSV_MAX_ROWS
    )
    try:
        max_rows = int(configured)
    except (TypeError, ValueError):
        max_rows = usage_ledger.EXPORT_CSV_MAX_ROWS
    max_rows = max(1, min(max_rows, usage_ledger.EXPORT_CSV_MAX_ROWS))

    chunks, truncated, total = usage_ledger.export_csv_chunks(
        user_id=current_user.id,
        workspace_id=request.args.get("workspace_id", type=int),
        project_id=request.args.get("project_id", type=int),
        source=request.args.get("source"),
        max_rows=max_rows,
        currency=get_currency_config()["code"],
    )
    response = Response(stream_with_context(iter(chunks)), mimetype="text/csv")
    response.headers["Content-Disposition"] = 'attachment; filename="usage.csv"'
    response.headers["Cache-Control"] = "no-store"
    response.headers["X-Export-Rows"] = str(min(total, max_rows))
    if truncated:
        response.headers["X-Export-Truncated"] = "true"
    return response


@bp.route("/usage/top", methods=["GET"])
@_login_required
@_rate_limit("usage")
def usage_top():
    """Return the current user's most expensive conversations and projects.

    The authenticated user is always the outer scope; an optional ``limit`` is
    bounded and defaults to ten. Ordering is deterministic: cost, tokens, id.
    """
    limit = request.args.get("limit", type=int)
    return jsonify(usage_ledger.top_entities(user_id=current_user.id, limit=limit or 10))


@bp.route("/usage/export", methods=["GET"])
@_login_required
@_rate_limit("usage")
def usage_export():
    """Export the current user's usage ledger as bounded, paginated JSON.

    The authenticated user is always the outer scope: ``workspace_id``,
    ``project_id`` and ``source`` only narrow that user's own records and can
    never expose another account's usage. ``page`` and ``limit`` are clamped to
    the same bounds as the marketplace listing (page >= 1, limit 1-500), and
    invalid values fall back to the defaults rather than returning an unbounded
    document.

    The payload includes ``schema_version``, ``exported_at``, the applied
    ``filters``, ``pagination`` metadata, ledger ``totals`` (plus per-page
    ``page_totals``), and ``records`` serialized with ``UsageRecord.to_dict``.
    Only metadata and token/cost counts are exported; prompts and completions
    are never stored in the ledger.
    """
    page = request.args.get("page", type=int)
    if page is None or page < 1:
        page = 1
    limit = request.args.get("limit", type=int)
    if limit is None:
        limit = usage_ledger.EXPORT_DEFAULT_LIMIT
    limit = max(1, min(limit, usage_ledger.EXPORT_MAX_LIMIT))

    payload = usage_ledger.export_payload(
        user_id=current_user.id,
        workspace_id=request.args.get("workspace_id", type=int),
        project_id=request.args.get("project_id", type=int),
        source=request.args.get("source"),
        page=page,
        limit=limit,
    )
    payload["currency"] = get_currency_config()
    payload["cost_is_estimate"] = True
    return jsonify(payload)
