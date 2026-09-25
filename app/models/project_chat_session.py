"""Named chat sessions for project conversations."""

from datetime import UTC, datetime

from app.extensions import db


class ProjectChatSession(db.Model):
    """A titled conversation belonging to one project."""

    __tablename__ = "project_chat_sessions"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    title = db.Column(db.String(200), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    project = db.relationship("Project", back_populates="chat_sessions")
    messages = db.relationship(
        "ProjectMessage",
        back_populates="session",
        cascade="all, delete-orphan",
        order_by="ProjectMessage.created_at",
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }
