"""Shared pytest fixtures."""

import pytest

from app import create_app
from app.extensions import db as _db


@pytest.fixture(autouse=True)
def _reset_event_dispatcher():
    """Clear the shared event dispatcher after each test.

    The global dispatcher is a module-level singleton; without a reset,
    subscriptions from one test would leak into the next (and fail
    authorization against a fresh database).
    """
    yield
    from app.services.events import get_dispatcher

    get_dispatcher().clear()


@pytest.fixture()
def app():
    """Create a fresh application instance for each test."""
    app = create_app("testing")

    with app.app_context():
        _db.create_all()
        yield app
        _db.session.remove()
        _db.drop_all()
        _db.engine.dispose()


@pytest.fixture()
def client(app):
    """A test client bound to the test application."""
    return app.test_client()


@pytest.fixture()
def db(app):
    """The SQLAlchemy extension bound to the test application."""
    return _db


@pytest.fixture()
def make_user(db):
    """Create and return a user with a known password."""
    from app.models import User

    def _make(username="tester", email="tester@example.com", password="supersecret123"):
        user = User(username=username, email=email)
        user.set_password(password)
        db.session.add(user)
        db.session.commit()
        return user

    return _make


@pytest.fixture()
def login(client):
    """Log a user in via the login form, switching away from any prior user."""

    def _login(email="tester@example.com", password="supersecret123"):
        client.post("/auth/logout")
        client.post(
            "/auth/login",
            data={"email": email, "password": password},
            follow_redirects=True,
        )

    return _login
