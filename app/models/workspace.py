"""AI workspace model.

A workspace is a user-owned space that groups imported projects (local
archives or GitHub repositories) so the AI assistant can explore, search, and
analyze them. Every workspace belongs to exactly one user; there is no shared
access, which keeps project content strictly isolated between accounts.
"""

import re
from datetime import UTC, datetime

from app.extensions import db


def generate_slug(name: str) -> str:
    """Derive a URL-safe slug from a workspace name.

    The algorithm lowercases the name, replaces every run of non-alphanumeric
    characters with a single dash, and strips leading/trailing dashes.  The
    result is suitable for use in URLs and is deterministic for any given name.
    """
    slug = name.lower()
    slug = re.sub(r"[^a-z0-9]+", "-", slug)
    return slug.strip("-")


class Workspace(db.Model):
    """A user-owned container for project imports."""

    __tablename__ = "workspaces"
    __table_args__ = (db.UniqueConstraint("user_id", "slug", name="uq_workspace_user_slug"),)

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = db.Column(db.String(200), nullable=False)
    slug = db.Column(db.String(200), nullable=False, index=True)
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

    def __init__(self, **kwargs):
        # Auto-derive slug from name when not explicitly provided.
        if "slug" not in kwargs and "name" in kwargs:
            kwargs["slug"] = generate_slug(kwargs["name"])
        super().__init__(**kwargs)

    def to_dict(self) -> dict:
        """Serialize the workspace for JSON API responses."""
        return {
            "id": self.id,
            "name": self.name,
            "slug": self.slug,
            "description": self.description,
            "is_pinned": bool(self.is_pinned),
            "project_count": len(self.projects),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Workspace id={self.id} name={self.name!r}>"
