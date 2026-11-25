"""Minimal team grouping for custom role assignments (#599).

Teams are named groups inside a workspace. They do not add a second permission
system: membership only makes a user eligible for a team-scoped custom role, and
the central workspace capability matrix remains authoritative.
"""

from datetime import UTC, datetime

from app.extensions import db
from app.models.workspace_member import STATUS_ACTIVE, STATUS_REMOVED

STATUS_VALUES = (STATUS_ACTIVE, STATUS_REMOVED)


class Team(db.Model):
    """A named group of members inside one workspace."""

    __tablename__ = "teams"
    __table_args__ = (db.UniqueConstraint("workspace_id", "name", name="uq_teams_workspace_name"),)

    id = db.Column(db.Integer, primary_key=True)
    workspace_id = db.Column(
        db.Integer,
        db.ForeignKey("workspaces.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    name = db.Column(db.String(100), nullable=False)
    created_by_id = db.Column(
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

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "workspace_id": self.workspace_id,
            "name": self.name,
            "created_by_id": self.created_by_id,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<Team id={self.id} workspace_id={self.workspace_id} name={self.name!r}>"


class TeamMember(db.Model):
    """A user's membership in a team (soft removal preserves history)."""

    __tablename__ = "team_members"
    __table_args__ = (db.UniqueConstraint("team_id", "user_id", name="uq_team_members_team_user"),)

    id = db.Column(db.Integer, primary_key=True)
    team_id = db.Column(
        db.Integer, db.ForeignKey("teams.id", ondelete="CASCADE"), nullable=False, index=True
    )
    user_id = db.Column(
        db.Integer, db.ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    status = db.Column(db.String(20), nullable=False, default=STATUS_ACTIVE)
    joined_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    removed_at = db.Column(db.DateTime(timezone=True), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    @property
    def is_active_member(self) -> bool:
        return self.status == STATUS_ACTIVE

    def mark_removed(self) -> None:
        self.status = STATUS_REMOVED
        self.removed_at = datetime.now(UTC)

    def reactivate(self) -> None:
        self.status = STATUS_ACTIVE
        self.removed_at = None

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "team_id": self.team_id,
            "user_id": self.user_id,
            "status": self.status,
            "joined_at": self.joined_at.isoformat() if self.joined_at else None,
            "removed_at": self.removed_at.isoformat() if self.removed_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<TeamMember id={self.id} team_id={self.team_id} user_id={self.user_id}>"
