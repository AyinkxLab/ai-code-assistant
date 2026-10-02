"""Persisted uploaded file (issue #42).

Uploads are written to disk under a configurable ``UPLOAD_FOLDER`` with a
random ``stored_name`` and only the validated original filename, size, and
content type are recorded. Keeping the bytes on disk (rather than in a BLOB)
lets the same file be downloaded, attached to a conversation, and re-analyzed
later without re-uploading.
"""

from datetime import UTC, datetime

from app.extensions import db


class StoredFile(db.Model):
    """A user-uploaded file persisted on disk and tracked in the ``files`` table."""

    __tablename__ = "files"

    id = db.Column(db.Integer, primary_key=True)
    #: The uploader. Every read/download is scoped to this user (owner-only).
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    #: Optional workspace the file belongs to.
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    #: Optional conversation the file has been attached to for re-analysis.
    conversation_id = db.Column(
        db.Integer,
        db.ForeignKey("conversations.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    original_name = db.Column(db.String(255), nullable=False)
    #: Random, collision-resistant on-disk name (never derived from user input).
    stored_name = db.Column(db.String(255), nullable=False, unique=True)
    size = db.Column(db.Integer, nullable=False, default=0)
    content_type = db.Column(db.String(100), nullable=False, default="application/octet-stream")
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    user = db.relationship("User")
    workspace = db.relationship("Workspace")
    conversation = db.relationship("Conversation")

    def to_dict(self) -> dict:
        """Serialize metadata only — never the stored path or raw bytes."""
        return {
            "id": self.id,
            "original_name": self.original_name,
            "size": self.size,
            "content_type": self.content_type,
            "workspace_id": self.workspace_id,
            "conversation_id": self.conversation_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "download_url": f"/files/{self.id}/download",
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<StoredFile id={self.id} original_name={self.original_name!r}>"
