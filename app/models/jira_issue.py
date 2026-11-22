"""Jira issue links for AI review findings (#680).

Each link records the external Jira issue key/id created for one review
finding. The unique ``(workspace_id, finding_id)`` constraint is the durable
duplicate guard: a second export of the same finding reuses the stored key and
never creates another Jira issue.
"""

from datetime import UTC, datetime

from app.extensions import db


class JiraIssueLink(db.Model):
    """A review finding that was exported to a Jira project."""

    __tablename__ = "jira_issue_links"
    __table_args__ = (
        db.UniqueConstraint(
            "workspace_id",
            "finding_id",
            name="uq_jira_issue_links_workspace_finding",
        ),
        db.UniqueConstraint("external_key", name="uq_jira_issue_links_external_key"),
        db.Index("ix_jira_issue_links_workspace_created", "workspace_id", "created_at"),
    )

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    review_id = db.Column(
        db.Integer,
        db.ForeignKey("reviews.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    finding_id = db.Column(
        db.Integer,
        db.ForeignKey("review_findings.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    project_key = db.Column(db.String(50), nullable=False)
    external_key = db.Column(db.String(100), nullable=False, index=True)
    external_id = db.Column(db.String(100), nullable=True)
    severity = db.Column(db.String(20), nullable=False)
    priority = db.Column(db.String(30), nullable=False)
    created_by_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="SET NULL"),
        nullable=True,
    )
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "review_id": self.review_id,
            "finding_id": self.finding_id,
            "project_key": self.project_key,
            "external_key": self.external_key,
            "external_id": self.external_id,
            "severity": self.severity,
            "priority": self.priority,
            "created_by_id": self.created_by_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<JiraIssueLink id={self.id} workspace_id={self.workspace_id} "
            f"external_key={self.external_key!r}>"
        )
