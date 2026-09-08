"""User account model and authentication helpers."""

from datetime import UTC, datetime

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from app.extensions import db, login_manager


class User(UserMixin, db.Model):
    """An application account.

    Passwords are never stored in plain text; only a salted hash produced by
    :func:`werkzeug.security.generate_password_hash` is persisted.
    """

    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    email = db.Column(db.String(255), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_active = db.Column(db.Boolean, nullable=False, default=True)
    is_admin = db.Column(db.Boolean, nullable=False, default=False)
    #: Optional per-user Stellar network selection (one of the supported
    #: selectable networks). ``None`` means "use the operator-configured
    #: ``STELLAR_NETWORK`` default" — mainnet is never an implicit default.
    stellar_network = db.Column(db.String(20), nullable=True)
    created_at = db.Column(
        db.DateTime(timezone=True), nullable=False, default=lambda: datetime.now(UTC)
    )
    last_login_at = db.Column(db.DateTime(timezone=True), nullable=True)
    updated_at = db.Column(
        db.DateTime(timezone=True),
        nullable=False,
        default=lambda: datetime.now(UTC),
        onupdate=lambda: datetime.now(UTC),
    )

    # -- Password handling -------------------------------------------------

    def set_password(self, password: str) -> None:
        """Hash and store ``password`` on the instance."""
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        """Return ``True`` when ``password`` matches the stored hash."""
        return check_password_hash(self.password_hash, password)

    # -- Convenience helpers ----------------------------------------------

    def touch_last_login(self) -> None:
        """Record the current time as the last successful login."""
        self.last_login_at = datetime.now(UTC)

    def to_dict(self) -> dict:
        """Serialize the user for JSON API responses (future phases)."""
        return {
            "id": self.id,
            "username": self.username,
            "email": self.email,
            "is_active": self.is_active,
            "is_admin": self.is_admin,
            "stellar_network": self.stellar_network,
            "created_at": self.created_at.isoformat() if self.created_at else None,
            "last_login_at": self.last_login_at.isoformat() if self.last_login_at else None,
        }

    def __repr__(self) -> str:  # pragma: no cover - debugging aid
        return f"<User id={self.id} username={self.username!r}>"


@login_manager.user_loader
def load_user(user_id: str) -> User | None:
    """Callback Flask-Login uses to load the current user for each request."""
    return db.session.get(User, int(user_id))
