"""Notification integration API and inbound email blueprint (#678-#684)."""

from flask import Blueprint

bp = Blueprint("integrations", __name__, url_prefix="/integrations")

from app.integrations import routes  # noqa: E402,F401  (register routes on import)
