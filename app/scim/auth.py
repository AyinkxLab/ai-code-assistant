"""Standalone SCIM bearer-token guard (#592).

Only paths under ``/scim/v2`` are handled here. The guard accepts *only*
``Authorization: Bearer <scim token>``: session cookies, ``X-API-Token``, and
personal API tokens are never accepted. It sets request-scoped token metadata
but deliberately does not set ``g._login_user`` or call Flask-Login, so a SCIM
request never becomes a browser session.
"""

from __future__ import annotations

from flask import g, request

from app.scim.errors import scim_error
from app.services import scim_tokens

SCIM_PATH_PREFIX = "/scim/v2"


def is_scim_path(path: str) -> bool:
    """Return ``True`` for the SCIM blueprint's URL space."""
    return path == SCIM_PATH_PREFIX or path.startswith(f"{SCIM_PATH_PREFIX}/")


def init_scim_auth(app) -> None:
    """Install the SCIM token guard as an application ``before_request`` hook."""

    @app.before_request
    def _authenticate_scim_request():
        g.scim_token = None
        g.scim_owner_id = None
        g.scim_workspace_id = None
        if not is_scim_path(request.path):
            return None

        header = request.headers.get("Authorization", "")
        if not header.lower().startswith("bearer "):
            return scim_error(401, "A SCIM bearer token is required.")
        secret = header[7:].strip()
        token = scim_tokens.authenticate(secret)
        if token is None:
            return scim_error(401, "The SCIM bearer token is invalid, expired, or revoked.")

        g.scim_token = token
        g.scim_owner_id = token.owner_id
        g.scim_workspace_id = token.workspace_id
        return None


__all__ = ["SCIM_PATH_PREFIX", "init_scim_auth", "is_scim_path"]
