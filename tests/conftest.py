"""Shared pytest fixtures."""

import pytest

from app import create_app
from app.extensions import db as _db


@pytest.fixture(scope="session")
def mock_stellar():
    """Boot the deterministic offline mock Stellar network once per session.

    Yields the running :class:`MockStellarServer`; point a ``custom`` network
    config's ``STELLAR_HORIZON_URL`` / ``STELLAR_RPC_URL`` at
    ``server.horizon_url`` / ``server.rpc_url`` to exercise the real service
    and RPC client code paths offline.
    """
    from app.services.stellar_mock import MockStellarServer

    server = MockStellarServer().start()
    try:
        yield server
    finally:
        server.stop()


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


@pytest.fixture(autouse=True)
def _reset_rate_limiter():
    """Clear the shared in-memory rate limiter around each test.

    The limiter is a module-level singleton, so without a reset hits recorded by
    one test would count against the next and could trip the per-user Phase 5
    limits (import/chat/analyze) in unrelated tests.
    """
    from app.services import ratelimit

    ratelimit.reset()
    yield
    ratelimit.reset()


@pytest.fixture(autouse=True)
def _reset_github_response_cache():
    """Clear the shared GitHub response cache around each test.

    The ETag cache is a module-level singleton, so without a reset an entry
    cached by one test would be replayed (and its 304 consumed) by the next,
    masking the very request the later test expects to make.
    """
    from app.services import github_cache

    github_cache.reset()
    yield
    github_cache.reset()


@pytest.fixture(autouse=True)
def _reset_marketplace_cache():
    """Clear the process-local last-known-good marketplace catalog cache.

    The cache is keyed by source but shared across tests, so a successful remote
    load in one test would otherwise be served as a stale fallback in the next
    test's failure case.
    """
    from app.services import marketplace

    marketplace.reset_marketplace_cache()
    yield
    marketplace.reset_marketplace_cache()


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


@pytest.fixture(autouse=True)
def _reset_alert_state():
    """Clear alert dedupe / dispatch state around each test.

    Alert services may keep process-local memory of already-emitted notifications
    (dedupe keys). Reset it so alerts emitted in one test do not suppress the
    same alert in another. The reset is best-effort: modules that do not expose
    a reset hook are simply skipped.
    """
    import importlib

    resetters = []
    for module_name in (
        "app.services.alerts",
        "app.services.budget_alerts",
        "app.services.budgets",
    ):
        try:
            module = importlib.import_module(module_name)
        except ImportError:  # optional module: nothing to reset
            continue
        reset = getattr(module, "reset", None)
        if callable(reset):
            resetters.append(reset)

    # Real-time presence is process-local too.
    try:
        from app.collaboration import realtime

        resetters.append(realtime.reset_presence)
    except ImportError:  # pragma: no cover - optional dependency
        pass

    for reset in resetters:
        reset()
    yield
    for reset in resetters:
        reset()
