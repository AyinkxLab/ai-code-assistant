"""GitLab OAuth and repository listing (#677).

Tokens are exchanged and used only in memory; the caller stores them through
``integration_credentials`` (AES-GCM). Errors never include the token, and
repository listing is bounded.
"""

from __future__ import annotations

from urllib.parse import urlencode

import requests
from flask import current_app


class GitlabError(Exception):
    """Base error with a stable code."""

    code = "gitlab_error"


class GitlabConfigError(GitlabError):
    """GitLab OAuth is not configured."""

    code = "gitlab_not_configured"


class GitlabAuthError(GitlabError):
    """GitLab rejected the code or token."""

    code = "gitlab_auth_failed"


class GitlabApiError(GitlabError):
    """GitLab returned an unexpected response."""

    code = "gitlab_api_error"


def _config(name: str, default=None):
    return current_app.config.get(name, default)


def is_configured() -> bool:
    """Whether a client id and secret are present."""
    return bool(_config("GITLAB_CLIENT_ID") and _config("GITLAB_CLIENT_SECRET"))


def _require_config() -> None:
    if not is_configured():
        raise GitlabConfigError("GitLab OAuth is not configured.")


def _timeout() -> float:
    return float(_config("GITLAB_REQUEST_TIMEOUT", 30))


def authorize_url(state: str) -> str:
    """Build the GitLab OAuth authorize URL."""
    _require_config()
    params = {
        "client_id": _config("GITLAB_CLIENT_ID"),
        "redirect_uri": _config("GITLAB_REDIRECT_URI"),
        "response_type": "code",
        "scope": _config("GITLAB_SCOPES", "read_api"),
        "state": state,
    }
    return f"{_config('GITLAB_OAUTH_AUTHORIZE_URL')}?{urlencode(params)}"


def exchange_code(code: str) -> str:
    """Exchange an OAuth code for an access token."""
    _require_config()
    code = str(code or "").strip()
    if not code:
        raise GitlabAuthError("An OAuth code is required.")
    try:
        response = requests.post(
            _config("GITLAB_OAUTH_TOKEN_URL"),
            data={
                "client_id": _config("GITLAB_CLIENT_ID"),
                "client_secret": _config("GITLAB_CLIENT_SECRET"),
                "code": code,
                "grant_type": "authorization_code",
                "redirect_uri": _config("GITLAB_REDIRECT_URI"),
            },
            timeout=_timeout(),
        )
    except requests.RequestException as exc:
        raise GitlabApiError("Unable to reach GitLab.") from exc
    if response.status_code >= 400:
        raise GitlabAuthError("GitLab rejected the authorization code.")
    try:
        token = response.json().get("access_token")
    except ValueError as exc:
        raise GitlabApiError("GitLab returned a malformed token response.") from exc
    if not isinstance(token, str) or not token:
        raise GitlabAuthError("GitLab did not return an access token.")
    return token


def _authorized_get(token: str, path: str, params: dict | None = None):
    try:
        response = requests.get(
            f"{_config('GITLAB_API_URL').rstrip('/')}{path}",
            headers={"Authorization": f"Bearer {token}"},
            params=params,
            timeout=_timeout(),
        )
    except requests.RequestException as exc:
        raise GitlabApiError("Unable to reach GitLab.") from exc
    if response.status_code in (401, 403):
        raise GitlabAuthError("GitLab rejected the access token.")
    if response.status_code >= 400:
        raise GitlabApiError(f"GitLab returned HTTP {response.status_code}.")
    try:
        return response.json()
    except ValueError as exc:
        raise GitlabApiError("GitLab returned malformed JSON.") from exc


def fetch_user(token: str) -> dict:
    """Return the authenticated GitLab user's public metadata."""
    data = _authorized_get(token, "/user")
    if not isinstance(data, dict):
        raise GitlabApiError("GitLab returned an unexpected user response.")
    return {
        "id": data.get("id"),
        "username": data.get("username"),
        "name": data.get("name"),
    }


def list_repositories(token: str, *, per_page: int = 50, max_pages: int = 3) -> list[dict]:
    """List the user's projects, bounded by page and repository limits."""
    per_page = max(1, min(int(per_page), 100))
    max_pages = max(1, min(int(max_pages), 10))
    limit = max(1, int(_config("GITLAB_MAX_REPOSITORIES", 100)))
    repositories: list[dict] = []
    for page in range(1, max_pages + 1):
        data = _authorized_get(
            token,
            "/projects",
            {"membership": "true", "per_page": per_page, "page": page},
        )
        if not isinstance(data, list):
            raise GitlabApiError("GitLab returned an unexpected project list.")
        for project in data:
            if not isinstance(project, dict):
                continue
            repositories.append(
                {
                    "id": project.get("id"),
                    "name": project.get("name"),
                    "path_with_namespace": project.get("path_with_namespace"),
                    "web_url": project.get("web_url"),
                    "visibility": project.get("visibility"),
                }
            )
            if len(repositories) >= limit:
                return repositories
        if len(data) < per_page:
            break
    return repositories


def revoke_token(token: str) -> bool:
    """Best-effort token revocation; returns whether GitLab confirmed it."""
    if not is_configured() or not token:
        return False
    try:
        response = requests.post(
            _config("GITLAB_OAUTH_REVOKE_URL"),
            data={
                "client_id": _config("GITLAB_CLIENT_ID"),
                "client_secret": _config("GITLAB_CLIENT_SECRET"),
                "token": token,
            },
            timeout=_timeout(),
        )
    except requests.RequestException:
        return False
    return response.status_code < 400


__all__ = [
    "GitlabApiError",
    "GitlabAuthError",
    "GitlabConfigError",
    "GitlabError",
    "authorize_url",
    "exchange_code",
    "fetch_user",
    "is_configured",
    "list_repositories",
    "revoke_token",
]
