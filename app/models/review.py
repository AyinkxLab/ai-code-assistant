"""AI review model.

A review captures a single AI review run against either a GitHub pull request
(``source="github_pr"``) or an imported project (``source="project"``, with a
```kind`` of quality/security/tests). The structured summary is stored as JSON
and the individual findings as ``ReviewFinding`` rows, so the quality
dashboard and review history can be computed entirely from real data.

Reviews are owned by the user who created them (``user_id``) and, for project
reviews, belong to a single project. Access is always owner/membership-scoped
at the route layer.
"""

from datetime import UTC, datetime

from app.extensions import db

SOURCE_GITHUB_PR = "github_pr"
SOURCE_PROJECT = "project"

STATUS_RUNNING = "running"
STATUS_COMPLETED = "completed"
STATUS_FAILED = "failed"

# Project review kinds. PR reviews use ``kind="pr"``.
PROJECT_REVIEW_KINDS = ("quality", "security", "tests")


class Review(db.Model):
    """A single AI review of a pull request or imported project."""

    __tablename__ = "reviews"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    project_id = db.Column(
        db.Integer,
        db.ForeignKey("projects.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    source = db.Column(db.String(20), nullable=False)
    kind = db.Column(db.String(30), nullable=False, default="pr")
    status = db.Column(db.String(20), nullable=False, default=STATUS_RUNNING)
    error_message = db.Column(db.Text, nullable=True)

    # GitHub pull request fields (only for source="github_pr").
    owner = db.Column(db.String(100), nullable=True)
    repo = db.Column(db.String(100), nullable=True)
    pr_number = db.Column(db.Integer, nullable=True)
    pr_title = db.Column(db.String(500), nullable=True)
    base_ref = db.Column(db.String(200), nullable=True)
    head_ref = db.Column(db.String(200), nullable=True)

    # Structured review output: summary JSON + the configuration snapshot used.
    summary = db.Column(db.Text, nullable=True)
    config = db.Column(db.Text, nullable=True)
    findings_count = db.Column(db.Integer, nullable=False, default=0)

    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    project = db.relationship("Project", back_populates="reviews")
    findings = db.relationship(
        "ReviewFinding",
        back_populates="review",
        cascade="all, delete-orphan",
        order_by="ReviewFinding.id",
    )

    @property
    def summary_dict(self) -> dict | None:
        """The parsed summary, or ``None`` when unavailable."""
        if not self.summary:
            return None
        import json

        try:
            data = json.loads(self.summary)
        except (ValueError, TypeError):
            return None
        return data if isinstance(data, dict) else None

    def to_dict(self) -> dict:
        """Serialize the review for JSON API responses."""
        return {
            "id": self.id,
            "user_id": self.user_id,
            "project_id": self.project_id,
            "source": self.source,
            "kind": self.kind,
            "status": self.status,
            "error_message": self.error_message,
            "owner": self.owner,
            "repo": self.repo,
            "pr_number": self.pr_number,
            "pr_title": self.pr_title,
            "base_ref": self.base_ref,
            "head_ref": self.head_ref,
            "summary": self.summary_dict,
            "findings_count": self.findings_count,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Review id={self.id} source={self.source!r} kind={self.kind!r}>"
