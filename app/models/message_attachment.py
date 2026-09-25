"""Chat message image attachment model (issue #49).

Images attached to a user message are stored as bounded binary blobs so the
chat UI can render them inline and the provider layer can forward them to
vision-capable models. Only image media types within the configured size cap
are ever persisted; the upload route enforces that before a row is created.
"""

from datetime import UTC, datetime

from app.extensions import db

#: Image media types accepted for chat attachments.
ALLOWED_IMAGE_TYPES = ("image/png", "image/jpeg", "image/webp")


class MessageAttachment(db.Model):
    """An image attached to a single chat message."""

    __tablename__ = "message_attachments"

    id = db.Column(db.Integer, primary_key=True)
    #: The conversation the image was uploaded against. Kept so an upload can
    #: be scoped to its owner before it is linked to a message.
    conversation_id = db.Column(
        db.Integer,
        db.ForeignKey("conversations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message_id = db.Column(
        db.Integer,
        db.ForeignKey("messages.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    filename = db.Column(db.String(255), nullable=False)
    content_type = db.Column(db.String(50), nullable=False)
    size = db.Column(db.Integer, nullable=False, default=0)
    data = db.Column(db.LargeBinary, nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    message = db.relationship("Message", back_populates="attachments")

    def to_dict(self) -> dict:
        """Serialize metadata only — never the raw bytes."""
        return {
            "id": self.id,
            "filename": self.filename,
            "content_type": self.content_type,
            "size": self.size,
            "url": f"/chat/attachments/{self.id}",
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<MessageAttachment id={self.id} filename={self.filename!r}>"
