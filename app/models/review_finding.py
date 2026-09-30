"""Review finding model.

A single structured finding produced by an AI review. Findings carry the
standardized severity/category vocabulary plus the model's confidence
(confirmed/potential/suggestion) and only ever reference the repository file
path and line — never raw repository content.
"""

from datetime import UTC, datetime

from app.extensions import db

SEVERITIES = ("critical", "high", "medium", "low", "informational")

# Per-review-kind category vocabularies.
PR_CATEGORIES = (
    "bug",
    "security",
    "logic",
    "performance",
    "validation",
    "tests",
    "maintainability",
    "other",
)
QUALITY_CATEGORIES = (
    "readability",
    "complexity",
    "long-function",
    "duplication",
    "dead-code",
    "error-handling",
    "unused-code",
    "maintainability",
    "consistency",
    "other",
)
SECURITY_CATEGORIES = (
    "authentication",
    "authorization",
    "input-validation",
    "file-access",
    "secrets",
    "injection",
    "unsafe-deserialization",
    "unsafe-dependencies",
    "information-exposure",
    "insecure-config",
    "other",
)
TEST_CATEGORIES = (
    "coverage-gap",
    "missing-tests",
    "missing-assertion",
    "flaky-test",
    "edge-case",
    "weak-coverage",
    "outdated-test",
    "test-structure",
    "other",
)

CONFIDENCES = ("confirmed", "potential", "suggestion")

# Confidence values that represent a confirmed defect (as opposed to a suggestion).
CONFIRMED_CONFIDENCES = ("confirmed",)

# Human-facing label for each confidence level (#111): a finding the code proves
# is [CONFIRMED]; anything inferred or uncertain is [SUGGESTION].
CONFIDENCE_LABELS = {
    "confirmed": "[CONFIRMED]",
    "potential": "[SUGGESTION]",
    "suggestion": "[SUGGESTION]",
}


def is_confirmed(confidence: str | None) -> bool:
    """Return ``True`` when a confidence value denotes a confirmed defect."""
    return (confidence or "").strip().lower() in CONFIRMED_CONFIDENCES


def confidence_label(confidence: str | None) -> str:
    """Return the ``[CONFIRMED]``/``[SUGGESTION]`` label for a confidence value."""
    return CONFIDENCE_LABELS.get((confidence or "").strip().lower(), "[SUGGESTION]")


CATEGORIES_BY_KIND = {
    "pr": PR_CATEGORIES,
    "quality": QUALITY_CATEGORIES,
    "security": SECURITY_CATEGORIES,
    "tests": TEST_CATEGORIES,
}


class ReviewFinding(db.Model):
    """A single finding from an AI review."""

    __tablename__ = "review_findings"

    id = db.Column(db.Integer, primary_key=True)
    review_id = db.Column(
        db.Integer, db.ForeignKey("reviews.id", ondelete="CASCADE"), nullable=False, index=True
    )
    file = db.Column(db.String(2000), nullable=True)
    line = db.Column(db.Integer, nullable=True)
    severity = db.Column(db.String(20), nullable=False, default="medium")
    category = db.Column(db.String(50), nullable=False, default="other")
    explanation = db.Column(db.Text, nullable=False)
    recommendation = db.Column(db.Text, nullable=True)
    confidence = db.Column(db.String(20), nullable=False, default="suggestion")
    addressed = db.Column(db.Boolean, nullable=False, default=False)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    review = db.relationship("Review", back_populates="findings")

    @property
    def confidence_label(self) -> str:
        """The ``[CONFIRMED]``/``[SUGGESTION]`` label for this finding."""
        return confidence_label(self.confidence)

    @property
    def is_confirmed(self) -> bool:
        """Whether this finding is a confirmed defect rather than a suggestion."""
        return is_confirmed(self.confidence)

    @property
    def action_item(self) -> str:
        """A copyable, GitHub-ready action item for this finding.

        The text is intentionally self-contained so it can be pasted directly
        into a GitHub review comment. It never includes raw repository content,
        only the file path, line, and the model's explanation/recommendation.
        """
        location = self.file or "repository"
        if self.line is not None:
            location = f"{location}:{self.line}"
        parts = [f"{self.confidence_label} [{self.severity}/{self.category}] {location}"]
        if self.explanation:
            parts.append(self.explanation.strip())
        if self.recommendation:
            parts.append(f"Recommendation: {self.recommendation.strip()}")
        return "\n".join(parts)

    def to_dict(self) -> dict:
        """Serialize the finding for JSON API responses."""
        return {
            "id": self.id,
            "review_id": self.review_id,
            "file": self.file,
            "line": self.line,
            "severity": self.severity,
            "category": self.category,
            "explanation": self.explanation,
            "recommendation": self.recommendation,
            "confidence": self.confidence,
            "confidence_label": self.confidence_label,
            "is_confirmed": self.is_confirmed,
            "action_item": self.action_item,
            "addressed": bool(self.addressed),
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<ReviewFinding id={self.id} file={self.file!r} "
            f"severity={self.severity!r} confidence={self.confidence!r}>"
        )
