"""Project file model.

Stores sanitized metadata for every file in an imported project plus, for
plain-text files under the size cap, a bounded copy of the file content that
powers project-wide search and AI context retrieval. Binary or oversized files
are stored with ``content=None`` so search and analysis can skip them.
"""

from datetime import UTC, datetime

from app.extensions import db


class ProjectFile(db.Model):
    """A single file inside an imported project."""

    __tablename__ = "project_files"
    __table_args__ = (
        db.UniqueConstraint("project_id", "path", name="uq_project_files_project_path"),
    )

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer, db.ForeignKey("projects.id", ondelete="CASCADE"), nullable=False, index=True
    )
    path = db.Column(db.String(2000), nullable=False, index=True)
    size = db.Column(db.Integer, nullable=False, default=0)
    is_binary = db.Column(db.Boolean, nullable=False, default=False)
    language = db.Column(db.String(50), nullable=True)
    content = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    project = db.relationship("Project", back_populates="files")

    def to_dict(self) -> dict:
        """Serialize file metadata (never the raw content)."""
        return {
            "id": self.id,
            "path": self.path,
            "size": self.size,
            "is_binary": self.is_binary,
            "language": self.language,
            "searchable": self.content is not None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<ProjectFile id={self.id} path={self.path!r}>"
