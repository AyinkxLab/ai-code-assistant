"""Chat message model."""

from enum import Enum
from datetime import UTC, datetime

from app.extensions import db

ERROR_STATUSES = ("retrying", "failed")


class ErrorStatus(str, Enum):
    """Provider failure states persisted on a message."""

    RETRYING = "retrying"
    FAILED = "failed"

    def __str__(self) -> str:  # pragma: no cover - convenience
        return self.value


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
    # Provider failure state (issue: retry/backoff). ``None`` for healthy
    # messages; ``"retrying"`` while a bounded retry is in flight and
    # ``"failed"`` once retries are exhausted or a permanent error occurred.
    error_status = db.Column(db.String(20), nullable=True)
    error_message = db.Column(db.Text, nullable=True)
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

    @property
    def is_failed(self) -> bool:
        """Return ``True`` when this message carries a terminal error state."""
        return self.error_status == ErrorStatus.FAILED.value

    @property
    def is_retrying(self) -> bool:
        """Return ``True`` while a bounded retry is in flight for this message."""
        return self.error_status == ErrorStatus.RETRYING.value

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
            "error": (
                {
                    "status": self.error_status,
                    "message": self.error_message,
                }
                if self.error_status
                else None
            ),
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Message id={self.id} role={self.role!r}>"
