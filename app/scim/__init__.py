"""SCIM 2.0 API blueprint (#592).

The SCIM surface is authenticated by its own organization bearer token guard
(:mod:`app.scim.auth`); it never uses Flask-Login or the personal API-token
guard. Importing this package registers the routes as a side effect, matching
the other Flask blueprints in this application.
"""

from flask import Blueprint

bp = Blueprint("scim", __name__, url_prefix="/scim/v2")

from app.scim import routes  # noqa: E402, F401
