"""GitLab OAuth integration blueprint (#677)."""

from flask import Blueprint

bp = Blueprint("gitlab", __name__, url_prefix="/gitlab")

from app.gitlab import routes  # noqa: E402,F401
