"""Persisted upload model (issue #42).

Uploaded files are written to disk under the configurable ``UPLOAD_FOLDER`` and
tracked in the ``files`` table so they can be listed, downloaded, attached to a
conversation, and re-analyzed later without re-uploading. Metadata lives in the
database; the bytes live on disk keyed by the random ``stored_name``.
"""

from datetime import UTC, datetime

from app.extensions import db


class UploadedFile(db.Model):
    """A file uploaded by a user and stored on disk."""

    __tablename__ = "files"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    #: Optional conversation association so an upload can be reused across
    #: sessions. ``SET NULL`` keeps the stored file if its conversation is
    #: deleted.
    conversation_id = db.Column(
        db.Integer,
        db.ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    original_name = db.Column(db.String(255), nullable=False)
    stored_name = db.Column(db.String(255), nullable=False, unique=True, index=True)
    size = db.Column(db.Integer, nullable=False, default=0)
    content_type = db.Column(db.String(255), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    workspace = db.relationship("Workspace")
    user = db.relationship("User")
    conversation = db.relationship("Conversation")

    def to_dict(self) -> dict:
        """Serialize the metadata (never the on-disk path or bytes)."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "user_id": self.user_id,
            "conversation_id": self.conversation_id,
            "original_name": self.original_name,
            "stored_name": self.stored_name,
            "size": self.size,
            "content_type": self.content_type,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<UploadedFile id={self.id} original_name={self.original_name!r}>"
