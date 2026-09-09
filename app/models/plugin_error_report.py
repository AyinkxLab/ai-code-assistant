"""Structured plugin error reports.

A bounded, safe record of a plugin failure (dispatch handler, lifecycle hook,
or management operation). Each row stores only small facts:

* plugin id and the operation that failed (e.g. ``dispatch:project.created``,
  ``hook:on_enable``);
* the exception type and a **length-bounded, sanitized** message (never a stack
  trace by default, never event payloads, never secrets);
* an optional workspace id so workspace-scoped failures can be read back by the
  workspace owner; rows without a workspace are operator-level (CLI) reads.

Recording is best-effort and never affects the (already isolated) failure.
"""

from datetime import UTC, datetime

from app.extensions import db

#: Maximum characters stored for an error message.
MAX_ERROR_MESSAGE_CHARS = 2000


class PluginErrorReport(db.Model):
    """A single bounded record of a plugin failure."""

    __tablename__ = "plugin_error_reports"

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    plugin_id = db.Column(db.String(64), nullable=False, index=True)
    operation = db.Column(db.String(160), nullable=False)
    exception_type = db.Column(db.String(200), nullable=True)
    message = db.Column(db.Text, nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )

    def to_dict(self) -> dict:
        """Serialize a report for API/CLI output (no stack, bounded message)."""
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "plugin_id": self.plugin_id,
            "operation": self.operation,
            "exception_type": self.exception_type,
            "message": self.message,
            "created_at": self.created_at.isoformat() if self.created_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return (
            f"<PluginErrorReport id={self.id} plugin={self.plugin_id!r} "
            f"operation={self.operation!r}>"
        )
