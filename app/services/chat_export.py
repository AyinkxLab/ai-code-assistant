"""Render chat conversations as portable Markdown documents (issue #46).

Message bodies are stored verbatim as the text the user typed or the provider
streamed back — the client renders and sanitises them for display only. An
export must therefore forward the stored text untouched: escaping it again here
would double-escape entities (``&`` becoming ``&amp;amp;``) and pull the fences
out of code blocks.

This module deliberately only adds document scaffolding — a title, metadata and
per-message headings — around the original content, in strict conversation
order, so fenced code blocks (including their language info strings) survive the
round trip unchanged.
"""

from __future__ import annotations

from app.models import Conversation, Message

#: Human-readable headings for the roles the app stores. Unknown roles fall back
#: to a capitalised version of the stored name.
_ROLE_LABELS = {"user": "User", "assistant": "Assistant", "system": "System"}


def _role_label(role: str) -> str:
    """Return a readable ``##`` heading label for a message ``role``."""
    known = _ROLE_LABELS.get((role or "").lower())
    if known is not None:
        return known
    return (role or "Message").capitalize()


def ordered_messages(conversation: Conversation) -> list[Message]:
    """Return a conversation's messages in deterministic conversation order.

    The relationship is already ordered by ``created_at`` in SQL, but two
    messages written in the same instant can share a timestamp and would then
    come back in an unspecified order. ``id`` is monotonically increasing, so it
    breaks those ties and preserves the real exchange order.
    """
    return sorted(
        conversation.messages,
        key=lambda message: (
            message.created_at.timestamp() if message.created_at else 0.0,
            message.id or 0,
        ),
    )


def render_conversation_markdown(conversation: Conversation) -> str:
    """Render ``conversation`` as a Markdown document.

    User messages and assistant replies are emitted in order under ``##``
    headings. An empty conversation still produces a valid document. Content is
    never escaped or rewritten, so fenced code blocks in the stored text are
    preserved exactly as written.
    """
    lines: list[str] = [f"# {conversation.title or 'Conversation'}", ""]

    meta = [
        ("Conversation ID", str(conversation.id)),
        ("Created", conversation.created_at.isoformat() if conversation.created_at else None),
        ("Updated", conversation.updated_at.isoformat() if conversation.updated_at else None),
        ("Messages", str(len(conversation.messages))),
    ]
    for label, value in meta:
        if value:
            lines.append(f"- **{label}:** {value}")
    lines.append("")

    messages = ordered_messages(conversation)
    if not messages:
        lines += ["*This conversation has no messages.*", ""]
        return "\n".join(lines)

    for message in messages:
        lines.append(f"## {_role_label(message.role)}")
        lines.append("")
        # Emit the stored content verbatim; only trailing newlines are trimmed so
        # exactly one blank line separates messages. Leading whitespace and code
        # fences are left untouched.
        lines.append((message.content or "").rstrip("\n"))
        lines.append("")

    return "\n".join(lines)
