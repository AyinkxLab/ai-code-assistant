"""AI workspace model.

A workspace is a user-owned space that groups imported projects (local
archives or GitHub repositories) so the AI assistant can explore, search, and
analyze them. Every workspace belongs to exactly one user; there is no shared
access, which keeps project content strictly isolated between accounts.

Each workspace also carries a ``slug``: a normalized (lowercased, dash
separated) form of its name that is unique per user. The slug is stored
explicitly in its own column rather than being recomputed on read, so it is a
real, indexed value the database can enforce; ``uq_workspaces_user_slug`` is
the source of truth for uniqueness and the API surfaces a 400 before it is hit.
"""

import re
from datetime import UTC, datetime

from sqlalchemy.orm import validates

from app.extensions import db

#: Stored slug width. Kept equal to the name column so a slug can always hold
#: a normalized version of the full name.
WORKSPACE_SLUG_MAX_LENGTH = 200

#: Used when a name has no slug-safe characters at all (e.g. "!!!" or "???").
WORKSPACE_SLUG_FALLBACK = "workspace"

_DASHES_RE = re.compile(r"-{2,}")


def slugify_workspace_name(name: str) -> str:
    """Normalize a workspace name into a URL-safe slug.

    Lowercases, replaces every run of non-alphanumeric characters with a single
    dash, trims leading/trailing dashes, and caps the result at
    ``WORKSPACE_SLUG_MAX_LENGTH``. Names that normalize to nothing (e.g. "!!!")
    fall back to ``WORKSPACE_SLUG_FALLBACK`` so the column is never empty.

    Because "My Workspace" and "my  workspace" normalize identically, the slug
    is what makes workspace names unique per user.
    """
    normalized = "".join(ch if ch.isalnum() else "-" for ch in (name or "").lower())
    slug = _DASHES_RE.sub("-", normalized).strip("-")
    slug = slug[:WORKSPACE_SLUG_MAX_LENGTH].strip("-")
    return slug or WORKSPACE_SLUG_FALLBACK


class Workspace(db.Model):
    """A user-owned container for project imports."""

    __tablename__ = "workspaces"
    __table_args__ = (
        # A slug is only unique within one account: two different users may
        # each own a workspace called "My Workspace".
        db.UniqueConstraint("user_id", "slug", name="uq_workspaces_user_slug"),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = db.Column(db.String(200), nullable=False)
    # URL-safe, normalized name. Stored (not derived on read) so links stay
    # valid across renames and the database can enforce per-user uniqueness.
    slug = db.Column(db.String(WORKSPACE_SLUG_MAX_LENGTH), nullable=False, index=True)
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

    @validates("name")
    def _sync_slug(self, key: str, value: str) -> str:
        """Re-derive the slug whenever the name is assigned.

        Keeping this on the model means the stored slug can never drift from
        the current name, whether the rename came from the API, a script, or
        a test fixture. It does not fire when a workspace is loaded from the
        database, so already-persisted slugs are left untouched.
        """
        self.slug = slugify_workspace_name(value)
        return value

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
