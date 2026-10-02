"""Persisted file uploads (issue #42)."""

from flask import Blueprint

bp = Blueprint("files", __name__, url_prefix="/files")

from app.files import routes  # noqa: E402,F401  (register routes on the blueprint)
