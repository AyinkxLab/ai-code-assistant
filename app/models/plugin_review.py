"""Marketplace ratings and reviews (#692).

One review per user per plugin, enforced by a unique constraint. A review can
be edited (re-rated) by its author. Moderation is deliberately minimal and
auditable: any signed-in user may report a review once with a reason, and the
plugin publisher (or an admin) may post one owner response.
"""

from datetime import UTC, datetime

from app.extensions import db


class PluginReview(db.Model):
    """A single marketplace rating/review for a plugin."""

    __tablename__ = "plugin_reviews"

    id = db.Column(db.Integer, primary_key=True)
    plugin_id = db.Column(
        db.String(64),
        db.ForeignKey("plugins.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id = db.Column(
        db.Integer,
        db.ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    rating = db.Column(db.Integer, nullable=False)
    title = db.Column(db.String(200), nullable=True)
    body = db.Column(db.Text, nullable=True)

    reported_at = db.Column(db.DateTime(timezone=True), nullable=True)
    report_reason = db.Column(db.Text, nullable=True)
    owner_response = db.Column(db.Text, nullable=True)
    owner_response_at = db.Column(db.DateTime(timezone=True), nullable=True)
    owner_response_by_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )

    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    user = db.relationship("User", foreign_keys=[user_id], lazy="select")
    owner_responder = db.relationship("User", foreign_keys=[owner_response_by_id])

    __table_args__ = (
        db.UniqueConstraint("plugin_id", "user_id", name="uq_plugin_review_user"),
        db.CheckConstraint("rating >= 1 AND rating <= 5", name="ck_plugin_review_rating"),
    )

    def to_dict(self) -> dict:
        """Serialize the review for API and page rendering."""
        return {
            "id": self.id,
            "plugin_id": self.plugin_id,
            "user_id": self.user_id,
            "username": self.user.username if self.user else None,
            "rating": self.rating,
            "title": self.title,
            "body": self.body,
            "reported": self.reported_at is not None,
            "report_reason": self.report_reason,
            "owner_response": self.owner_response,
            "owner_response_at": (
                self.owner_response_at.isoformat() if self.owner_response_at else None
            ),
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PluginReview plugin={self.plugin_id!r} user={self.user_id}>"
