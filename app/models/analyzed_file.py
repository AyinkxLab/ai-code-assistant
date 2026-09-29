"""Private identity for source files submitted to the analysis tool."""

from datetime import UTC, datetime

from app.extensions import db


class AnalyzedFile(db.Model):
    """An uploaded file identity; file contents are deliberately not stored."""

    __tablename__ = "analyzed_files"
    __table_args__ = (
        db.UniqueConstraint(
            "user_id", "filename", "content_hash", name="uq_analyzed_file_owner_name_hash"
        ),
    )

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    filename = db.Column(db.String(255), nullable=False)
    content_hash = db.Column(db.String(64), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    analyses = db.relationship(
        "FileAnalysis", back_populates="file", cascade="all, delete-orphan"
    )
