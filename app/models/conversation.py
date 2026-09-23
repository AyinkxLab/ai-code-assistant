"""Chat conversation model."""

from datetime import UTC, datetime

from app.extensions import db


class Conversation(db.Model):
    """A chat session belonging to a single user.

    A conversation groups the messages exchanged with the AI assistant and
    carries metadata (title, pinned status) used by the chat UI.
    """

    __tablename__ = "conversations"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = db.Column(db.String(200), nullable=False, default="New conversation")
    is_pinned = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    messages = db.relationship(
        "Message",
        back_populates="conversation",
        cascade="all, delete-orphan",
        order_by="Message.created_at",
    )
    shares = db.relationship(
        "ConversationShare",
        back_populates="conversation",
        cascade="all, delete-orphan",
    )

    def is_shared_with(self, user_id: int) -> bool:
        """Return ``True`` when the conversation was shared with ``user_id``."""
        return any(share.user_id == user_id for share in self.shares)

    def to_dict(self) -> dict:
        """Serialize the conversation for JSON API responses."""
        return {
            "id": self.id,
            "title": self.title,
            "is_pinned": self.is_pinned,
            "is_shared": len(self.shares) > 0,
            "message_count": len(self.messages),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Conversation id={self.id} title={self.title!r}>"
