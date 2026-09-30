"""GitHub API service layer.

A thin, retrying client for the GitHub REST API used by the repository
browser, issues, pull requests, and chat context features. All requests are
made on behalf of the current user's connected GitHub account, so GitHub's own
permissions model decides which repositories (public or private)
are
accessible.

Errors are raised as :class:`GitHubError` subclasses so routes can translate
them into user-facing responses without swallowing failures.
"""

from __future__ import annotations

import base64
import json
import logging
import re
import time
from dataclasses import dataclass, field
from datetime import UTD, 
datetime, timedelta
from urllib.parse import parse_qs, urlparse

import requests
from flask import current_app

from app.config import Config
from app.extensions import db
from app.models import GithubAccount
from app.services.crypto import decrypt_secret

logger = logging.getLogger(__name__)

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
GITHUB_REVOKE_URL = "https://api.github.com/applications/{client_id}/token"
DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"

# GitHub repository names / owners: letters, digits, dashes, dots, underscores.
_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

#: Default and maximum ``er_page`` for list endpoints (GitHub caps at 100).
PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 100

#: Matches one ``<url>; rel="name"``
entry of a GitHub ``Link`` header.
_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="([^"]+)"')

#: Matches file paths mentioned in an issue body.
# Examples: `src/app.py`, `app/services/github.py`, `docs/README.md`.
# Requires at least one `/` separator and a file extension to avoid
# matching prose like "and/or" or version numbers.
_FILE_PATH_RE = re.compile(
    r"""(?<![\w/.-])              # not preceded by a path character
    (?:[\w.-]+/)+              # one or more directory
    [\w.-]+                    # file name
    \.[A-Za-z0-9]{1,10}         # extension
    (?![\w/.-])                # not followed by a path character
    """,
    re.VERBOSE,
)

#: Maximum number of files to fetch for a single issue analysis.
MAX_ISSUE_FILES = 10

#: Maximum bytes read from a single file when building context.
MAX_FILE_CONTENT_CHARS = 20000


def extract_file_paths(text: str | None, *, limit: int = MAX_ISSUE_FILES) -> list[str]:
    """Return unique file paths mentioned in ``text``, in order of appearance.

    Only path-like tokens (at least one directory separator and a
    file extension) are returned. Leading trailing punctuation and
    common GitHub URL forms are normalized away.
    """
    if not text:
        return []

    seen: list[str] = []
    seen_set: set[str] = set()
    for match in _FILE_PATH_RE.finditer(text):
        path = match.group(0).strip("`'".",")")
        path = path.strip().lstrip("/")
        if not path or path in seen_set:
            continue
        seen_set.add(path)
        seen.append(path)
        if len(seen) >= limit:
            break
    return seen


@dataclass(frozen=True)
class GitHubPage:
    """A single page of a GitHub list endpoint plus its navigation metadata.

    `ahas_next` / ``has_prev`` / ``total_pages`` are derived from the ``Link``
    response header GitHub returns, so the caller never has to guess where the
    next page is.
    """

    items: list[dict]
    page: int
    per_page: int
    has_next: bool = False
    has_prev: bool = False
    total_pages: int | None = None


def parse_link_header(header: str) -> dict[str, str]:
    """Return ``{rel: url}`` parsed from a GitHub ``Link`` header."""
    return {rel: url for url, rel in _LINK_RE.findall(header or "")}


def _page_number_from_url(url: str | None) -> int | None:
    """Extract the ``page`` query parameter from a GitHub paging URL."""
    if not url:
        return None
    values = parse_qs(urlparse(url).query).get("page")
    if not values:
        return None
    try:
        return int(values[0])
    except (TypeError, ValueError):
        return None


def _github_config(name: str):
    """Read GitHub settings from the active app, with class-config fallback."""
    return current_app.config.get(name, getattr(Config, name))


class GitHubError(RuntimeError):
    """Base class for GitHub API errors surfaced to the user."""

    kind = "github_error"

    def __init__(self, message: str, *, kind: str | None = None, detail: str | None = None) -> None:
        super().__init__(message)
        if kind is not None:
            self.kind = kind
        # Server-side-only detail (e.g. GitHub's raw response message). Never
        # surfaced to the client; logged instead. See ``github_error_payload``.
        self.detail = detail


class GitHubNotConnectedError(GitHubError):
    kind = "not_connected"


class GitHubAuthError(GitHubError):
    kind = "auth"


class GitHubPermissionError(GitHubError):
    kind = "permission"


class GitHubNotFoundError(GitHubError):
    kind = "not_found"


class GitHubRateLimitError(GitHubError):
    kind = "rate_limit"


class GitHubNetworkError(GitHubError):
    kind = "network"


class GitHubInvalidError(GitHubError):
    kind = "validation"


#: Stable, sanitized messages shown to end users, keyed by error ``kind``.
#: GitHub's raw response bodies are never surfaced: they can contain URLs,
#: headers, rate-limit data, or implementation details. Those are kept on
#: ``GitHubError.detail`` for server-side logging only.
ERROR_MESSAGES = {
    "not_connected": "Connect your GitHub account to use this feature.",
    "auth": "Your GitHub connection is no longer valid. Reconnect your account.",
    "permission": "GitHub denied access to this resource.",
    "not_found": "The requested GitHub resource was not found.",
    "rate_limit": "GitHub API rate limit reached. Please try again later.",
    "network": "Could not reach the GitHub API. Please try again.",
    "validation": "The GitHub request was invalid.",
    "github_error": "The GitHub request failed. Please try again.",
}


def github_error_message(exc: GitHubError) -> str:
    """Return the stable user-facing message for ``exc``'s kind."""
    return ERROR_MESSAGES.get(exc.kind, ERROR_MESSAGES["github_error"])


def github_error_payload(exc: GitHubError) -> dict:
    """Build a sanitized JSON error payload for a :class:`GitHubError`.

    Route handlers must use this instead of ``str(exc)`` so no raw GitHub
    message (or token/header material) is ever echoed to the client.
    """
    return {"error": github_error_message(exc), "kind": exc.kind}


def _parse_error_body(response: requests.Response) -> str:
    """Extract a short human-readable message for server-side logging only."""
    try:
        data = response.json()
    except ValueError:
        return response.text[:200] or f"HTTP {response.status_code}"
    if isinstance(data, dict):
        message = data.get("message")
        if isinstance(message, str):
            return message
    return f"HTTP {response.status_code}"


class GitHubClient:
    """Authenticated client for the GitHub REST API.

    Provides bounded retry behaviour: transient failures (network errors and
    HTTP 5xx) are retried with exponential backoff, and rate-limit responses
    (429 or 403 with exhausted quota) pause until the documented reset time.
    """

    def __init__(
        self,
        access_token: str,
        *,
        api_url: str | None = None,
        timeout: int | None = None,
        max_retries: int = 3,
    ) -> None:
        self.api_url = (api_url or Config.GITHUB_API_URL).rstrip("/")
        self.timeout = timeout or Config.GITHUB_REQUEST_TIMEOUT
        self.max_retries = max_retries
        self.session = requests.Session()
        self.session.headers.update(
            {
                "Authorization": f"Bearer {access_token}",
                "Accept": "application/vnd.github+json",
                "X-GitHub-Api-Version": API_VERSION,
            }
        )

    # -- Core request machinery -----------------------------------------

    def _request(self, method: str, path: str, *, params: dict | None = None) -> dict | list:
        """Perform a request with retries, raising typed errors on failure."""
        url = f"{self.api_url}{}".format(path)
        last_exc: Exception | None = None
        for attempt in range(self.max_retries):
            try:
                response = self.session.request(method, url, params=params, timeout=self.timeout)
            except requests.RequestException as exc:
                last_exc = exc
                logger.warning("GitHub network error on %s: %s", path, exc)
                if attempt < self.max_retries - 1:
                    time.sleep(2**attempt)
                    continue
                raise GitHubNetworkError(
                    "Could not reach the GitHub API. Please try again.", detail=str(exc)
                ) from exc

            if response.status_code == 404:
                raise GitHubNotFoundError("The requested GitHub resource was not found.")
            if response.status_code == 401:
                raise GitHubAuthError(
                    "Your GitHub connection is no longer valid. Reconnect your account."
                )

            if response.status_code in (403, 429):
                remaining = response.headers.get("X-RateLimit-Remaining", "")
                if remaining == "0" or response.status_code == 429:
                    reset_at = int(response.headers.get("X-RateLimit-Reset", "0"))
                    wait = max(reset_at - int(time.time()), 1)
                    raise GitHubRateLimitError(
                        f"GitHub API rate limit reached. Retry in about {wait} seconds."
                    )
                detail = _parse_error_body(response)
                logger.warning("GitHub permission error on %s: %s", path, detail)
                raise GitHubPermissionError("GitHub denied access to this resource.", detail=detail)

            if response.status_code >= 500:
                last_exc = GitHubError(f"GitHub API returned {response.status_code}.")
                if attempt < self.max_retries - 1:
                    time.sleep(2**attempt)
                    continue
                raise GitHubNetworkError(
                    f"GitHub API is experiencing issues (HTTP {response.status_code})."
                ) from last_exc

            if response.status_code >= 400:
                detail = _parse_error_body(response)
                logger.warning(
                    "GitHub API error on %s (%s): %s", path, response.status_code, detail
                )
                raise GitHubError("The GitHub request failed. Please try again.", detail=detail)

            if response.status_code == 204 or not response.content:
                return {}

            try:
                return response.json()
            except ValueError as exc:
                raise GitHubError("GitHub API returned an unexpected response.") from exc

        raise GitHubNetworkError(
            "Could not reach the GitHub API. Please try again.", detail=str(last_exc)
        )

    def _get(self, path: str, *, params: dict | None = None) -> dict | list:
        return self._request("GET", path, params=params)

    def _get_paginated(
        self, path: str, *, params: dict | None = None, max_items: int = 300
    ) -> list[dict]:
        """Fetch paginated results, following the ``Link`` header up to a cap."""
        items: list[dict] = []
        page_params = dict(params or {})
        page_params.setdefault("per_page", 100)
        url = f"{self.api_url}{path}"

        while url and len(items) < max_items:
            response = self.session.get(url, params=page_params, timeout=self.timeout)
            if response.status_code >= 400:
                return self._request("GET", path, params=params)  # translate error
            try:
                page: list[dict] = response.json()
            except ValueError:
                page = []
            items.extend(page)

            link = response.headers.get("Link", "")
            next_url = None
            for part in link.split(","):
                if 'rel="next"' in part:
                    next_url = part[part.find("<") + 1 : part.find(">")]
                    break
            if next_url is None or not page:
                break
            url = next_url
            page_params = None

        return items[:max_items]

    def _get_page(
        self,
        path: str,
        *,
        params: dict | None = None,
        page: int = 1,
        per_page: int = PAGE_SIZE_DEFAULT,
    ) -> GitHubPage:
        """Fetch one page of a GitHub list endpoint.

        ``page`` is 1-based and ``per_page`` is clamped to ``PAGE_SIZE_MAX``
        (GitHub's own maximum) so a single response is always bounded. The
        ``Link`` header GitHub sends back is parsed to report whether a
        next/previous page exists and, when GitHub includes it, the last page
        number.
        """
        page = max(1, int(page or 1))
        per_page = max(1, min(int(per_page or PAGE_SIZE_DEFAULT), PAGE_SIZE_MAX))
        request_params = dict(params or {})
        request_params.update({"page": page, "per_page": per_page})

        response = self.session.get(
            f"{self.api_url}{path}", params=request_params, timeout=self.timeout
        )
        if response.status_code >= 400:
            self._request("GET", path, params=request_params)  # translate error
        try:
            items: list[dict] = response.json()
        except ValueError:
            items = []

        links = parse_link_header(response.headers.get("Link", ""))
        total_pages = _page_number_from_url(links.get("last"))
        return GitHubPage(
            items=items,
            page=page,
            per_page=per_page,
            has_next="next" in links,
            has_prev="prev" in links,
            total_pages=total_pages,
        )

    # -- Repositories ---------------------------------------------------

    def list_repos(
        self,
        *,
        page: int = 1,
        per_page: int = PAGE_SIZE_DEFAULT,
        sort: str = "updated",
        affiliation: str = "owner,collaborator,organization_member",
    ) -> GitHubPage:
        """List repositories the authenticated user can access."""
        return self._get_page(
            "/user/repos",
            params={"sort": sort, "affiliation": affiliation},
            page=page,
            per_page=per_page,
        )

    def get_repo(self, full_name: str) -> dict:
        """Fetch a single repository by ``owner/name``."""
        _validate_full_name(full_name)
        return self._get(f"/repos/{full_name}")

    def list_branches(self, full_name: str, *, page: int = 1, per_page: int = PAGE_SIZE_DEFAULT) -> GitHubPage:
        """List branches for a repository."""
        _validate_full_name(full_name)
        return self._get_page(f"/repos/{full_name}/branches", page=page, per_page=per_page)

    def get_content(self, full_name: str, path: str, path_ref: str | None = None) -> dict:
        """Fetch a file or directory entry from a repository."""
        _validate_full_name(full_name)
        params = {"ref": path_ref} if path_ref else None
        return self._get(f"/repos/{full_name}/contents/{path.lstrip('/')}", params=params)

    def get_file_text(self, full_name: str, path: str, path_ref: str | None = None) -> str | None:
        """Return the decoded text of a file, or ``None`` if it is not text.

        Missing or inaccessible files return ``None`` rather than raising, so
        callers can degrade gracefully when building context for analysis.
        """
        try:
            data = self.get_content(full_name, path, path_ref)
        except GitHubError as exc:
            logger.info("Skipping file %s: %s", path, exc.kind)
            return None
        if not isinstance(data, dict) or data.get("type") != "file":
            return None
        encoding = data.get("encoding")
        content = data.get("content")
        if encoding == "base64" and isinstance(content, str):
            try:
                raw = base64.b64decode(content)
            except Exception:
                return None
            try:
                return raw.decode("utf-8")
            except UnicodeDecodeError:
                return None
        return None

    # -- Issues ----------------------------------------------------------

    def list_issues(
        self,
        full_name: str,
        *,
        state: str = "open",
        page: int = 1,
        per_page: int = PAGE_SIZE_DEFAULT,
    ) -> GitHubPage:
        """List issues for a repository (excluding pull requests)."""
        _validate_full_name(full_name)
        return self._get_page(
            f"/repos/{full_name}/issues",
            params={"state": state},
            page=page,
            per_page=per_page,
        )

    def get_issue(self, full_name: str, number: int) -> dict:
        """Fetch a single issue."""
        _validate_full_name(full_name)
        return self._get(f"/repos/{full_name}/issues/{int(number)}")

    def list_issue_comments(
        self,
        full_name: str,
        number: int,
        *,
        page: int = 1,
        per_page: int = PAGE_SIZE_DEFAULT,
    ) -> GitHubPage:
        """List comments on an issue."""
        _validate_full_name(full_name)
        return self._get_page(
            f"/repos/{full_name}/issues/{int(number)}/comments",
            page=page,
            per_page=per_page,
        )

    def create_issue_comment(self, full_name: str, number: int, body: str) -> dict:
        """Post a comment on an issue."""
        _validate_full_name(full_name)
        return self._request(
            "POST",
            f"/repos/{full_name}/issues/{int(number)}/comments",
            params={"body": body},
        )

    # -- Pull requests ---------------------------------------------------------

    def list_pulls(
        self,
        full_name: str,
        *,
        state: str = "open",
        page: int = 1,
        per_page: int = PAGE_SIZE_DEFAULT,
    ) -> GitHubPage:
        """List pull requests for a repository."""
        _validate_full_name(full_name)
        return self._get_page(
            f"/repos/{full_name}/pulls",
            params={"state": state},
            page=page,
            per_page=per_page,
        )

    def get_pull(self, full_name: str, number: int) -> dict:
        """Fetch a single pull request."""
        _validate_full_name(full_name)
        return self._get(f"/repos/{full_name}/pulls/{int(number)}")

    def list_pull_files(self, full_name: str, number: int) -> list[dict]:
        """List the files changed by a pull request."""
        _validate_full_name(full_name)
        return self._get_paginated(f"/repos/{full_name}/pulls/{int(number)}/files")

    def list_pull_comments(
        self,
        full_name: str,
        number: int,
        *,
        page: int = 1,
        per_page: int = PAGE_SIZE_DEFAULT,
    ) -> GitHubPage:
        """List review comments on a pull request."""
        _validate_full_name(full_name)
        return self._get_page(
            f"/repos/{full_name}/pulls/{int(number)}/comments",
            page=page,
            per_page=per_page,
        )

    def list_pull_reviews(
        self,
        full_name: str,
        number: int,
        *,
        page: int = 1,
        per_page: int = PAGE_SIZE_DEFAULT,
    ) -> GitHubPage:
        """List reviews on a pull request."""
        _validate_full_name(full_name)
        return self._get_page(
            f"/repos/{full_name}/pulls/{int(number)}/reviews",
            page=page,
            per_page=per_page,
        )

    def get_pull_diff(self, full_name: str, number: int) -> str:
        """Return the raw diff for a pull request."""
        _validate_full_name(full_name)
        response = self.session.get(
            f"{self.api_url}/repos/{full_name}/pulls/{int(number)}",
            headers={"Accept": "application/vnd.github.diff"},
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            self._request("GET", f"/repos/{full_name}/pulls/{int(number)}")
        return response.text


def _validate_full_name(full_name: str) -> None:
    """Raise :class:`GitHubInvalidError` if ``full_name`` is not ``owner/repo``."""
    if not isinstance(full_name, str) or not _FULL_NAME_RE.match(full_name):
        raise GitHubInvalidError("Invalid repository name.")


def get_client_for_user(user_id) -> GitHubClient:
    """Return an authenticated client for ``user_id``'s connected account."""
    account = GithubAccount.query.filter_by(user_id=user_id).first()
    if account is None or not account.access_token_encrypted:
        raise GitHubNotConnectedError(
            "Connect your GitHub account to use this feature."
        )
    token = decrypt_secret(account.access_token_encrypted)
    return GitHubClient(token)


def build_issue_context(
    client: GitHubClient,
    full_name: str,
    body: str | None,
    *,
    max_chars: int | None = None,
    max_files: int = MAX_ISSUE_FILES,
) -> dict:
    """Gather file content for paths mentioned in an issue body.

    Returns a dict with ``files```(a list of ``file`, ``content`` entries),
    ``skipped```(paths that could not be fetched), and ``chars_used``.
    The combined content is capped at ``max_chars```(see
    ``GITHUB_MAX_CONTEXT_CHARS``), and files are added in order of
    appearance until the budget is exhausted. Missing or inaccessible
    files are skipped without raising.
    """
    if max_chars is None:
        max_chars = _github_config("GITHUB_MAX_CONTEXT_CHARS")
    paths = extract_file_paths(body, limit=max_files)
    files: list[dict] = []
    skipped: list[str] = []
    chars_used = 0
    for path in paths:
        if chars_used >= max_chars:
            skipped.append(path)
            continue
        content = client.get_file_text(full_name, path)
        if content is None:
            skipped.append(path)
            continue
        remaining = max_chars - chars_used
        if len(content) > remaining:
            content = content[:remaining]
        files.append({
            "file": path,
            "content": content,
            "truncated": len(content) == remaining and remaining < max_chars,
        })
        chars_used += len(content)
    return {"files": files, "skipped": skipped, "chars_used": chars_used}
