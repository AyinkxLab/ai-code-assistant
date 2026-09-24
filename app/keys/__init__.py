"""LLM provider API key management blueprint (page + owner-scoped JSON API)."""

from flask import Blueprint

bp = Blueprint("keys", __name__, url_prefix="/keys")

from app.keys import routes  # noqa: E402, F401  (import routes to register them)
