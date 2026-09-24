"""AI workspace model.

A workspace is a user-owned space that groups imported projects (local
archives or GitHub repositories) so the AI assistant can explore, search, and
analyze them. Every workspace belongs to exactly one user; there is no shared
access, which keeps project content strictly isolated between accounts.
"""

from datetime import UTC, datetime

from app.extensions import db


class Workspace(db.Model):
    """A user-owned container for project imports."""

    __tablename__ = "workspaces"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = db.Column(db.String(200), nullable=False)
    description = db.Column(db.Text, nullable=True)
    # Pinned workspaces sort to the top of the dashboard (then by recent
    # activity). The flag is per workspace and toggled from the dashboard.
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

    projects = db.relationship(
        "Project",
        back_populates="workspace",
        cascade="all, delete-orphan",
        order_by="Project.created_at",
    )
    members = db.relationship(
        "WorkspaceMember",
        back_populates="workspace",
        cascade="all, delete-orphan",
        order_by="WorkspaceMember.created_at",
    )
    invitations = db.relationship(
        "WorkspaceInvitation",
        back_populates="workspace",
        cascade="all, delete-orphan",
        order_by="WorkspaceInvitation.created_at",
    )
    settings = db.relationship(
        "WorkspaceSettings",
        back_populates="workspace",
        cascade="all, delete-orphan",
        uselist=False,
    )

    def to_dict(self) -> dict:
        """Serialize the workspace for JSON API responses."""
        return {
            "id": self.id,
            "name": self.name,
            "description": self.description,
            "is_pinned": bool(self.is_pinned),
            "project_count": len(self.projects),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Workspace id={self.id} name={self.name!r}>"
