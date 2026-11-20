"""WSGI entry point for production servers (e.g. gunicorn).

Run with::

    gunicorn --bind 0.0.0.0:5000 wsgi:app
"""

import hmac
import os

from flask import abort, jsonify

from app import create_app

app = create_app()


@app.get("/__maintenance/migrate/<token>")
def run_database_migrations(token: str):
    """One-time, token-protected production migration bootstrap."""
    expected = os.getenv("MIGRATION_TOKEN", "")
    if not expected or not hmac.compare_digest(token, expected):
        abort(404)

    from flask_migrate import upgrade

    upgrade(directory="migrations")
    return jsonify({"ok": True, "message": "Database migrations applied."})
