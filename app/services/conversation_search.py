"""Search conversations and their messages for the current user."""

from __future__ import annotations

import re

from sqlalchemy import and_, or_

from app.extensions import db
from app.models import Conversation, Message


def _like_pattern(value: str) -> str:
    """Return a literal, parameter-bound LIKE pattern."""
    escaped = value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    return f"%{escaped}%"


def _snippet(content: str, query: str, context: int = 80) -> dict[str, str]:
    """Return text surrounding the first case-insensitive query match."""
    match = re.search(re.escape(query), content, flags=re.IGNORECASE)
    if match is None:  # Defensive fallback for title-only result rows.
        return {"before": "", "match": "", "after": content[: context * 2]}

    start = max(0, match.start() - context)
    end = min(len(content), match.end() + context)
    return {
        "before": ("…" if start else "") + content[start : match.start()],
        "match": content[match.start() : match.end()],
        "after": content[match.end() : end] + ("…" if end < len(content) else ""),
    }


def search_conversations(user_id: int, query: str) -> list[dict]:
    """Search owned conversation titles and message content.

    ``ilike`` receives a bound value through SQLAlchemy; no user input is
    interpolated into SQL. The owner predicate is applied to every row.
    """
    query = query.strip()
    if not query:
        return []

    pattern = _like_pattern(query)
    rows = (
        db.session.query(Conversation, Message)
        .outerjoin(
            Message,
            and_(
                Message.conversation_id == Conversation.id,
                Message.content.ilike(pattern, escape="\\"),
            ),
        )
        .filter(Conversation.user_id == user_id)
        .filter(
            or_(
                Conversation.title.ilike(pattern, escape="\\"),
                Message.id.isnot(None),
            )
        )
        .order_by(Conversation.updated_at.desc(), Message.created_at.asc())
        .all()
    )

    grouped: dict[int, dict] = {}
    for conversation, message in rows:
        result = grouped.setdefault(
            conversation.id,
            {
                "conversation_id": conversation.id,
                "title": conversation.title,
                "matches": [],
            },
        )
        if message is not None:
            result["matches"].append(
                {
                    "message_id": message.id,
                    "role": message.role,
                    "snippet": _snippet(message.content, query),
                }
            )

    return list(grouped.values())
