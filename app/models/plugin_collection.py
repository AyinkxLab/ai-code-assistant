"""Curated plugin collections for onboarding and security (#694).

A collection is maintainer-curated marketplace metadata: a stable slug, a
bounded display name and description, an ordered list of plugin ids, and a
``featured`` flag. It never installs anything and never grants a capability;
installing a plugin still goes through the signed, allowlisted install path.
"""

from datetime import UTC, datetime

from app.extensions import db


class PluginCollection(db.Model):
    """A maintainer-curated, ordered group of advertised plugin ids."""

    __tablename__ = "plugin_collections"

    id = db.Column(db.Integer, primary_key=True)
    slug = db.Column(db.String(64), unique=True, nullable=False, index=True)
    name = db.Column(db.String(120), nullable=False)
    #: Bounded (280 chars) maintainer-authored summary. Never executable code.
    description = db.Column(db.String(280), nullable=False, default="")
    featured = db.Column(db.Boolean, nullable=False, default=False, index=True)
    plugin_ids = db.Column(db.JSON, nullable=False, default=list)
    created_by_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    created_by = db.relationship("User", foreign_keys=[created_by_id], lazy="select")

    def to_dict(self) -> dict:
        """Serialize collection metadata (no plugin content or secrets)."""
        return {
            "id": self.id,
            "slug": self.slug,
            "name": self.name,
            "description": self.description,
            "featured": bool(self.featured),
            "plugin_ids": list(self.plugin_ids or []),
            "plugin_count": len(self.plugin_ids or []),
            "created_by_id": self.created_by_id,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "updated_at": self.updated_at.isoformat() if self.updated_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<PluginCollection {self.slug!r} featured={bool(self.featured)}>"
