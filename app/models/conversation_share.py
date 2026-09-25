"""Secure share links for conversation history."""

from datetime import UTC, datetime

from app.extensions import db


class ConversationShare(db.Model):
    __tablename__ = "conversation_shares"

    id = db.Column(db.Integer, primary_key=True)
    conversation_id = db.Column(
        db.Integer,
        db.ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    created_by = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False
    )
    token_hash = db.Column(db.String(64), nullable=False, unique=True, index=True)
    expires_at = db.Column(db.DateTime(timezone=True), nullable=False, index=True)
    permission = db.Column(db.String(20), nullable=False, default="read_only")
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    conversation = db.relationship("Conversation", backref="shares")

    def is_expired(self) -> bool:
        return self.expires_at <= datetime.now(UTC)
