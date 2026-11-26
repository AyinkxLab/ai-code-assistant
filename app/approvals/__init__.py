"""Sensitive-action approval API blueprint (#601)."""

from flask import Blueprint

bp = Blueprint("approvals", __name__, url_prefix="/api/approvals")

from app.approvals import routes  # noqa: E402,F401
