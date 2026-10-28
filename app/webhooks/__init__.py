"""Webhooks blueprint (Phase 12, #674)."""

from flask import Blueprint

bp = Blueprint("webhooks", __name__, url_prefix="/api/webhooks")

from app.webhooks import routes  # noqa: E402, F401
