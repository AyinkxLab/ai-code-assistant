"""Centralized Flask extension instances.

Extensions are created here and bound to the application inside the app
factory in ``app/__init__.py``. Keeping them separate from the factory avoids
circular imports and makes the extensions importable from models, routes, and
tests.
"""

from flask import g, request, session
from flask_login import LoginManager
from flask_migrate import Migrate
from flask_socketio import SocketIO
from flask_sqlalchemy import SQLAlchemy
from flask_wtf.csrf import CSRFProtect


class ApiTokenCSRFProtect(CSRFProtect):
    """Skip session CSRF for token-only API requests (#616).

    A request carrying a bearer/``X-API-Token`` header and no login session is
    authenticated by the token, not a cookie, so there is no CSRF session to
    protect. Session-authenticated requests keep the normal check.
    """

    def protect(self):  # pragma: no cover - exercised through request tests
        token_request = bool(
            request.headers.get("Authorization") or request.headers.get("X-API-Token")
        )
        if token_request and "_user_id" not in session:
            g.csrf_valid = True
            return
        return super().protect()


db = SQLAlchemy()
migrate = Migrate()
login_manager = LoginManager()
csrf = ApiTokenCSRFProtect()
# Real-time collaboration transport (issue #50). The server uses the threaded
# async mode so it works with the stock development server and tests; production
# deployments can select another mode through ``SOCKETIO_ASYNC_MODE``.
socketio = SocketIO()

# Where unauthenticated users are redirected.
login_manager.login_view = "auth.login"
login_manager.login_message = "Please log in to access this page."
login_manager.login_message_category = "warning"
