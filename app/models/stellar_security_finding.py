"""Structured findings from the Stellar/Soroban security analysis.

The ``stellar_security`` analysis returns a narrative plus, when the model
emits a bounded structured block, individual findings. Each finding is stored
as its own row so a developer can filter, view, and act on them instead of
reading one text blob.

Security rules mirror ``ReviewFinding``: findings only reference a repository
file path and line (never raw file content), severities use the shared
vocabulary, and the rows belong to a single ``project`` so reads are gated by
the same owner/membership authorization as every other project surface.
"""

from datetime import UTC, datetime

from app.extensions import db

SEVERITIES = ("critical", "high", "medium", "low", "informational")

#: Stellar/Soroban finding categories (from the analysis prompt guidance).
STELLAR_CATEGORIES = (
    "authorization",
    "admin-controls",
    "error-handling",
    "cross-contract",
    "secrets",
    "storage",
    "testing",
    "configuration",
    "other",
)

CONFIDENCES = ("confirmed", "potential", "suggestion")


class StellarSecurityFinding(db.Model):
    """A single persisted finding from a ``stellar_security`` analysis run."""

    __tablename__ = "stellar_security_findings"

    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    file = db.Column(db.String(2000), nullable=True)
    line = db.Column(db.Integer, nullable=True)
    severity = db.Column(db.String(20), nullable=False, default="medium")
    category = db.Column(db.String(50), nullable=False, default="other")
    confidence = db.Column(db.String(20), nullable=False, default="suggestion")
    evidence = db.Column(db.Text, nullable=True)
    explanation = db.Column(db.Text, nullable=False)
    recommendation = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    def to_dict(self) -> dict:
        """Serialize the finding for JSON API responses."""
        return {
            "id": self.id,
            "project_id": self.project_id,
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "category": self.category,
            "confidence": self.confidence,
            "evidence": self.evidence,
            "explanation": self.explanation,
            "recommendation": self.recommendation,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<StellarSecurityFinding id={self.id} project_id={self.project_id} "
            f"severity={self.severity!r} category={self.category!r}>"
        )
