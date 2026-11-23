"""Personal API token settings blueprint."""

from flask import Blueprint

bp = Blueprint("tokens", __name__, url_prefix="/settings/tokens")

from app.tokens import routes  # noqa: E402,F401  (registers the views)
