"""Minimal SCIM 2.0 Groups blueprint (#593)."""

from flask import Blueprint

bp = Blueprint("scim", __name__, url_prefix="/scim/v2")

from app.scim import routes  # noqa: E402,F401
