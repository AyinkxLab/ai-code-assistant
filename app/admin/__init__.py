"""Admin-only operational endpoints (audit log and organization console)."""

from flask import Blueprint

bp = Blueprint("admin", __name__, url_prefix="/admin")

from app.admin import (  # noqa: E402, F401  (import routes to register them)
    organization_dashboard,
    organization_members,
    organization_usage,
    routes,
)
