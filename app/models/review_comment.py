"""Inline code-review comment model (issue #52).

A ``ReviewComment`` is a discussion thread anchored to a specific assistant
message in a project chat — optionally to a fenced code block and/or a line
range within it. Threads support one level of replies, ``@username`` mentions
resolved against active workspace members, and a resolve/unresolve toggle.

Access mirrors the rest of the collaboration surface: active workspace members
may create comments; the workspace owner (or the comment's author) may resolve
or delete. Anchors store only positions (block index, line numbers) — never raw
source content.
"""

from __future__ import annotations

from datetime import UTC, datetime

from app.extensions import db

REVIEW_COMMENT_MAX_LENGTH = 4000


class ReviewComment(db.Model):
    """A single inline review comment (or reply) on an assistant message."""

    __tablename__ = "review_comments"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    message_id = db.Column(
        db.Integer,
        db.ForeignKey("project_messages.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    author_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    parent_id = db.Column(
        db.Integer, db.ForeignKey("review_comments.id", ondelete="CASCADE"), nullable=True
    )
    body = db.Column(db.Text, nullable=False)

    # Anchor: which fenced code block (0-based) and/or line range of the
    # assistant message the comment refers to. ``NULL`` means the whole message.
    block_index = db.Column(db.Integer, nullable=True)
    line_start = db.Column(db.Integer, nullable=True)
    line_end = db.Column(db.Integer, nullable=True)

    resolved = db.Column(db.Boolean, nullable=False, default=False)
    resolved_by = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    resolved_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    project = db.relationship("Project")
    message = db.relationship("ProjectMessage")
    author = db.relationship("User", foreign_keys=[author_id])
    resolver = db.relationship("User", foreign_keys=[resolved_by])
    replies = db.relationship(
        "ReviewComment",
        backref=db.backref("parent", remote_side=[id]),
        cascade="all, delete-orphan",
        order_by="ReviewComment.created_at",
    )

    def to_dict(self) -> dict:
        """Serialize the comment for API responses."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "message_id": self.message_id,
            "author_id": self.author_id,
            "author_username": self.author.username if self.author else None,
            "parent_id": self.parent_id,
            "body": self.body,
            "block_index": self.block_index,
            "line_start": self.line_start,
            "line_end": self.line_end,
            "resolved": bool(self.resolved),
            "resolved_by": self.resolved_by,
            "resolved_by_username": self.resolver.username if self.resolver else None,
            "resolved_at": self.resolved_at.isoformat() if self.resolved_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ReviewComment id={self.id} message_id={self.message_id}>"
