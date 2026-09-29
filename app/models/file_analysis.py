"""Per-user persisted results from the uploaded-file analysis tool."""

from datetime import UTC, datetime

from app.extensions import db


class FileAnalysis(db.Model):
    """A result snapshot for one user-owned uploaded file and action."""

    __tablename__ = "file_analyses"

    id = db.Column(db.Integer, primary_key=True)
    file_id = db.Column(
        db.Integer,
        db.ForeignKey("analyzed_files.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    action = db.Column(db.String(30), nullable=False)
    result = db.Column(db.Text, nullable=False)
    provider = db.Column(db.String(100), nullable=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC), index=True
    )

    file = db.relationship("AnalyzedFile", back_populates="analyses")

    def to_dict(self):
        return {
            "id": self.id,
            "file_id": self.file_id,
            "filename": self.file.filename if self.file else None,
            "action": self.action,
            "result": self.result,
            "provider": self.provider,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }
