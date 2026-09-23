"""Conversation share model.

Records that a conversation was explicitly shared with another user. Only the
conversation's owner can share it; a shared user gains read access and is
notified through a ``share`` notification. Sharing does not transfer ownership
or grant write access.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extensions import db


class ConversationShare(db.Model):
    """A single (conversation, user) share record."""

    __tablename__ = "conversation_shares"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer,
        db.ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    shared_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    __table_args__ = (
        db.UniqueConstraint("conversation_id", "user_id", name="uq_conversation_share_user"),
    )

    conversation = db.relationship("Conversation", back_populates="shares")
    shared_by = db.relationship("User", foreign_keys=[shared_by_id])

    def to_dict(self) -> dict:
        """Serialize the share for JSON API responses."""
        return {
            "id": self.id,
            "conversation_id": self.conversation_id,
            "user_id": self.user_id,
            "shared_by_id": self.shared_by_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ConversationShare conversation_id={self.conversation_id} user_id={self.user_id}>"
