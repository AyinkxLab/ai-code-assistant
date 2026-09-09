"""Imported project model.

A project is an imported snapshot of a codebase (from a GitHub repository or an
uploaded archive) belonging to a single workspace. ``status`` tracks whether
indexing has completed; ``file_count`` and ``total_size_bytes`` are the real
statistics computed from the stored ``ProjectFile`` rows.
"""

from datetime import UTC, datetime

from app.extensions import db

SOURCE_GITHUB = "github"
SOURCE_ARCHIVE = "archive"
SOURCE_SCAFFOLD = "scaffold"
VALID_SOURCES = (SOURCE_GITHUB, SOURCE_ARCHIVE, SOURCE_SCAFFOLD)

STATUS_INDEXING = "indexing"
STATUS_READY = "ready"
STATUS_FAILED = "failed"
VALID_STATUSES = (STATUS_INDEXING, STATUS_READY, STATUS_FAILED)


class Project(db.Model):
    """An indexed copy of a codebase owned by a single user."""

    __tablename__ = "projects"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    # Denormalized owner id so every query can be scoped to the current user
    # without a join, and so a project can never outlive its owner.
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    name = db.Column(db.String(200), nullable=False)
    source = db.Column(db.String(20), nullable=False, default=SOURCE_ARCHIVE)
    source_url = db.Column(db.String(500), nullable=True)
    status = db.Column(db.String(20), nullable=False, default=STATUS_INDEXING)
    error_message = db.Column(db.Text, nullable=True)
    file_count = db.Column(db.Integer, nullable=False, default=0)
    total_size_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    indexed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    workspace = db.relationship("Workspace", back_populates="projects")
    files = db.relationship(
        "ProjectFile",
        back_populates="project",
        cascade="all, delete-orphan",
        lazy="dynamic",
    )
    messages = db.relationship(
        "ProjectMessage",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectMessage.created_at",
    )
    reviews = db.relationship(
        "Review",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="Review.created_at",
    )
    review_config = db.relationship(
        "ReviewConfig",
        back_populates="project",
        cascade="all, delete-orphan",
        uselist=False,
    )
    comments = db.relationship(
        "ProjectComment",
        back_populates="project",
        cascade="all, delete-orphan",
        order_by="ProjectComment.created_at",
    )

    def to_dict(self) -> dict:
        """Serialize project metadata for JSON API responses."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "source": self.source,
            "source_url": self.source_url,
            "status": self.status,
            "error_message": self.error_message,
            "file_count": self.file_count,
            "total_size_bytes": self.total_size_bytes,
            "indexed_at": self.indexed_at.isoformat() if self.indexed_at else None,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Project id={self.id} name={self.name!r} status={self.status!r}>"
