"""Plugins blueprint: workspace-scoped plugin management.

Provides the user-facing layer over the Phase 8 plugin system: listing
registered plugins, inspecting metadata, installing trusted/local plugin
manifests, per-workspace enable/disable, and explicit capability grants.
Authorization reuses the Phase 7 workspace role model (owners manage,
members view). The backend is authoritative; the UI is never trusted for
authorization.
"""

from flask import Blueprint

bp = Blueprint("plugins", __name__, url_prefix="/plugins")

from app.plugins import routes  # noqa: E402,F401
