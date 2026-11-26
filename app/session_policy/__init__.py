"""Organization session policy API blueprint (#591)."""

from flask import Blueprint

bp = Blueprint("session_policy", __name__, url_prefix="/api/session-policy")

from app.session_policy import routes  # noqa: E402,F401
