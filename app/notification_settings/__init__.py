"""Workspace notification-channel preferences blueprint (#631)."""

from flask import Blueprint

bp = Blueprint("notification_settings", __name__, url_prefix="/workspaces")

from app.notification_settings import routes  # noqa: E402,F401
