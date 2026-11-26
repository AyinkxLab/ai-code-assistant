"""Organization group-to-role mapping API blueprint (#593)."""

from flask import Blueprint

bp = Blueprint("group_roles", __name__, url_prefix="/api/group-roles")

from app.group_roles import routes  # noqa: E402,F401
