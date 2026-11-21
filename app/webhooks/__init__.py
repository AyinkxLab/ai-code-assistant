"""Webhook delivery-log blueprint (Phase 12, #672)."""

from flask import Blueprint

bp = Blueprint("webhooks", __name__, url_prefix="/webhooks")

from app.webhooks import routes  # noqa: E402,F401  (register routes on import)
