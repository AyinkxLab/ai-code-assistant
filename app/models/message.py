"""Chat message model."""

from datetime import UTC, datetime

from app.extensions import db


class Message(db.Model):
    """A single message exchanged within a conversation.

    ``role`` is one of ``user`` or ``assistant``. Prompt text and assistant
    responses are stored verbatim so conversation history can be replayed or
    exported.
    """

    __tablename__ = "messages"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer,
        db.ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    role = db.Column(db.String(20), nullable=False)
    content = db.Column(db.Text, nullable=False)
    # Token usage recorded for the provider response that produced this message
    # (issue #13). ``None`` for user messages and for historical rows.
    prompt_tokens = db.Column(db.Integer, nullable=True)
    completion_tokens = db.Column(db.Integer, nullable=True)
    total_tokens = db.Column(db.Integer, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    conversation = db.relationship("Conversation", back_populates="messages")
    attachments = db.relationship(
        "MessageAttachment",
        back_populates="message",
        cascade="all, delete-orphan",
        order_by="MessageAttachment.created_at",
    )

    def to_dict(self) -> dict:
        """Serialize the message for JSON API responses."""
        usage = None
        if self.total_tokens is not None:
            usage = {
                "prompt_tokens": self.prompt_tokens or 0,
                "completion_tokens": self.completion_tokens or 0,
                "total_tokens": self.total_tokens or 0,
            }
        return {
            "id": self.id,
            "role": self.role,
            "content": self.content,
            "attachments": [attachment.to_dict() for attachment in self.attachments],
            "token_usage": usage,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Message id={self.id} role={self.role!r}>"
