"""GitHub API service layer.

A thin, retrying client for the GitHub REST API used by the repository
browser, issues, pull requests, and chat context features. All requests are
made on behalf of the current user's connected GitHub account, so GitHub's own
permissions model decides which repositories (public or private) are
accessible.

Errors are raised as :class:`GitHubError` subclasses so routes can translate
them into user-facing responses without swallowing failures.
"""

from __future__ import annotations

import base64
import logging
import re
import time

import requests

from app.config import Config
from app.models import GithubAccount
from app.services.crypto import decrypt_secret

logger = logging.getLogger(__name__)

GITHUB_AUTHORIZE_URL = "https://github.com/login/oauth/authorize"
GITHUB_TOKEN_URL = "https://github.com/login/oauth/access_token"
DEFAULT_API_URL = "https://api.github.com"
API_VERSION = "2022-11-28"

# GitHub repository names / owners: letters, digits, dashes, dots, underscores.
_FULL_NAME_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")


class GitHubError(RuntimeError):
    """Base class for GitHub API errors surfaced to the user."""

    kind = "github_error"

    def __init__(self, message: str, *, kind: str | None = None) -> None:
        super().__init__(message)
        if kind is not None:
            self.kind = kind


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


def _parse_error_body(response: requests.Response) -> str:
    """Extract a short human-readable message from an error response."""
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

    # -- Core request machinery --------------------------------------------

    def _request(self, method: str, path: str, *, params: dict | None = None) -> dict | list:
        """Perform a request with retries, raising typed errors on failure."""
        url = f"{self.api_url}{path}"
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
                raise GitHubNetworkError(f"Could not reach the GitHub API: {exc}") from exc

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
                raise GitHubPermissionError(f"GitHub denied access: {_parse_error_body(response)}")

            if response.status_code >= 500:
                last_exc = GitHubError(f"GitHub API returned {response.status_code}.")
                if attempt < self.max_retries - 1:
                    time.sleep(2**attempt)
                    continue
                raise GitHubNetworkError(
                    f"GitHub API is experiencing issues (HTTP {response.status_code})."
                ) from last_exc

            if response.status_code >= 400:
                raise GitHubError(
                    f"GitHub API error ({response.status_code}): {_parse_error_body(response)}"
                )

            if response.status_code == 204 or not response.content:
                return {}

            try:
                return response.json()
            except ValueError as exc:
                raise GitHubError("GitHub API returned an unexpected response.") from exc

        raise GitHubNetworkError(f"GitHub request failed after retries: {last_exc}")

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

    # -- GitHub account -----------------------------------------------------

    def get_user(self) -> dict:
        return self._get("/user")

    # -- Repositories -------------------------------------------------------

    def list_repositories(self, *, per_page: int = 100) -> list[dict]:
        return self._get_paginated(
            "/user/repos",
            params={"affiliation": "owner,collaborator", "sort": "updated", "per_page": per_page},
        )

    def get_repository(self, full_name: str) -> dict:
        return self._get(f"/repos/{full_name}")

    def list_branches(self, full_name: str) -> list[dict]:
        return self._get_paginated(f"/repos/{full_name}/branches")

    # -- Contents / tree ----------------------------------------------------

    def get_contents(
        self, full_name: str, path: str = "", ref: str | None = None
    ) -> list[dict] | dict:
        params = {}
        if ref:
            params["ref"] = ref
        return self._get(f"/repos/{full_name}/contents/{path}", params=params or None)

    def get_tree(self, full_name: str, ref: str, *, recursive: bool = True) -> dict:
        return self._get(
            f"/repos/{full_name}/git/trees/{ref}",
            params={"recursive": 1 if recursive else None},
        )

    def search_files(self, full_name: str, query: str, ref: str) -> list[dict]:
        """Return repository paths whose file name matches ``query`` (case-insensitive)."""
        tree = self.get_tree(full_name, ref)
        matches = []
        needle = query.strip().lower()
        for entry in tree.get("tree", []):
            if entry.get("type") != "blob":
                continue
            path = entry.get("path", "")
            if not needle or needle in path.lower():
                matches.append({"path": path, "size": entry.get("size")})
        return matches[:100]

    def get_file_text(self, full_name: str, path: str, ref: str | None = None) -> str:
        """Return the decoded UTF-8 text of a file, enforcing a size cap.

        Raises :class:`GitHubError` for binary or oversized files.
        """
        params = {}
        if ref:
            params["ref"] = ref
        response = self.session.get(
            f"{self.api_url}/repos/{full_name}/contents/{path}",
            params=params or None,
            timeout=self.timeout,
        )
        if response.status_code >= 400:
            self._request("GET", f"/repos/{full_name}/contents/{path}", params=params or None)
            raise GitHubError("Could not read file contents.")

        if response.headers.get("Content-Type", "").startswith("application/json"):
            try:
                data = response.json()
            except ValueError:
                raise GitHubError("Could not read file contents.") from None
            if isinstance(data, dict) and "content" in data:
                raw = base64.b64decode(data["content"])
                if len(raw) > Config.GITHUB_MAX_CONTEXT_CHARS * 2:
                    raise GitHubError("This file is too large to display or analyze.")
                return raw.decode("utf-8", errors="replace")
        raise GitHubError("This file is not a text file or is too large to display.")

    def get_file_text_batch(
        self,
        full_name: str,
        paths: list[str],
        ref: str | None = None,
        *,
        max_files: int = 25,
        max_chars: int = 200_000,
    ) -> list[dict]:
        """Fetch bounded text contents for a list of repo paths (best-effort).

        Used to assemble a small, detection-relevant slice of a repository for
        Stellar/Soroban analysis without downloading the whole repo. Files that
        are binary, oversized, or cannot be read are skipped silently so a
        failed fetch never breaks the analysis.

        Returns ``[{"path": str, "content": str}, ...]`` with at most
        ``max_files`` entries, each content capped at ``max_chars`` characters.
        """
        rows: list[dict] = []
        for path in paths:
            if len(rows) >= max_files:
                break
            try:
                text = self.get_file_text(full_name, path, ref=ref)
            except GitHubError:
                continue
            if text is None:
                continue
            rows.append({"path": path, "content": text[:max_chars]})
        return rows

    # -- Commits ------------------------------------------------------------

    def list_commits(
        self, full_name: str, *, ref: str | None = None, path: str | None = None
    ) -> list[dict]:
        params = {"per_page": 50}
        if ref:
            params["sha"] = ref
        if path:
            params["path"] = path
        return self._get(f"/repos/{full_name}/commits", params=params)

    def get_commit(self, full_name: str, sha: str) -> dict:
        return self._get(f"/repos/{full_name}/commits/{sha}")

    # -- Issues -------------------------------------------------------------

    def list_issues(self, full_name: str, *, state: str = "open", per_page: int = 50) -> list[dict]:
        """Return issues (excluding pull requests)."""
        data = self._get(
            f"/repos/{full_name}/issues",
            params={"state": state, "per_page": per_page},
        )
        return [item for item in data if "pull_request" not in item]

    def get_issue(self, full_name: str, number: int) -> dict:
        return self._get(f"/repos/{full_name}/issues/{number}")

    # -- Pull requests ------------------------------------------------------

    def list_pull_requests(self, full_name: str, *, state: str = "open") -> list[dict]:
        return self._get(
            f"/repos/{full_name}/pulls",
            params={"state": state, "per_page": 50},
        )

    def get_pull_request(self, full_name: str, number: int) -> dict:
        return self._get(f"/repos/{full_name}/pulls/{number}")

    def list_pull_request_files(self, full_name: str, number: int) -> list[dict]:
        return self._get(f"/repos/{full_name}/pulls/{number}/files", params={"per_page": 100})

    def list_pull_request_reviews(self, full_name: str, number: int) -> list[dict]:
        return self._get(f"/repos/{full_name}/pulls/{number}/reviews")

    def list_pull_request_comments(self, full_name: str, number: int) -> list[dict]:
        return self._get(f"/repos/{full_name}/pulls/{number}/comments", params={"per_page": 100})

    # -- README -------------------------------------------------------------

    def get_readme(self, full_name: str, ref: str | None = None) -> str | None:
        params = {}
        if ref:
            params["ref"] = ref
        try:
            response = self.session.get(
                f"{self.api_url}/repos/{full_name}/readme",
                params=params or None,
                headers={"Accept": "application/vnd.github.raw+json"},
                timeout=self.timeout,
            )
        except requests.RequestException:
            return None
        if response.status_code != 200:
            return None
        text = response.text
        return text[: Config.GITHUB_MAX_CONTEXT_CHARS] if text else None


# -- Payload normalization ---------------------------------------------------


def repo_payload(repo: dict) -> dict:
    """Normalize a repository dict into the shape consumed by the UI."""
    return {
        "full_name": repo.get("full_name"),
        "name": repo.get("name"),
        "description": repo.get("description"),
        "owner": repo.get("owner", {}).get("login"),
        "visibility": repo.get("visibility", "private" if repo.get("private") else "public"),
        "private": bool(repo.get("private")),
        "default_branch": repo.get("default_branch"),
        "language": repo.get("language"),
        "updated_at": repo.get("updated_at"),
        "html_url": repo.get("html_url"),
        "size": repo.get("size"),
        "fork": bool(repo.get("fork")),
    }


def issue_payload(issue: dict) -> dict:
    return {
        "number": issue.get("number"),
        "title": issue.get("title"),
        "body": issue.get("body"),
        "state": issue.get("state"),
        "author": (issue.get("user") or {}).get("login"),
        "created_at": issue.get("created_at"),
        "updated_at": issue.get("updated_at"),
        "labels": [label.get("name") for label in issue.get("labels", [])],
        "html_url": issue.get("html_url"),
        "comments": issue.get("comments", 0),
    }


def commit_payload(commit: dict) -> dict:
    commit_data = commit.get("commit") or {}
    author = commit_data.get("author") or {}
    return {
        "sha": commit.get("sha"),
        "message": commit_data.get("message"),
        "author": author.get("name") or (commit.get("author") or {}).get("login"),
        "date": author.get("date"),
        "html_url": commit.get("html_url"),
        "url": commit.get("url"),
        "files": [
            {
                "filename": f.get("filename"),
                "additions": f.get("additions"),
                "deletions": f.get("deletions"),
                "status": f.get("status"),
                "patch": f.get("patch"),
            }
            for f in commit.get("files", [])
        ],
    }


def pull_request_payload(pr: dict) -> dict:
    return {
        "number": pr.get("number"),
        "title": pr.get("title"),
        "body": pr.get("body"),
        "state": pr.get("state"),
        "author": (pr.get("user") or {}).get("login"),
        "created_at": pr.get("created_at"),
        "updated_at": pr.get("updated_at"),
        "merged": bool(pr.get("merged")),
        "mergeable": pr.get("mergeable"),
        "head": (pr.get("head") or {}).get("ref"),
        "base": (pr.get("base") or {}).get("ref"),
        "additions": pr.get("additions"),
        "deletions": pr.get("deletions"),
        "changed_files": pr.get("changed_files"),
        "html_url": pr.get("html_url"),
        "diff_url": pr.get("diff_url"),
    }


def validate_full_name(full_name: str) -> str:
    """Validate ``owner/repo`` shape; raises :class:`GitHubInvalidError`."""
    name = (full_name or "").strip()
    if not _FULL_NAME_RE.match(name) or name.count("/") != 1:
        raise GitHubInvalidError("Invalid repository name.")
    return name


def validate_path(path: str) -> str:
    """Validate a repository-relative file path against path traversal.

    An empty/root path is accepted and normalized to ``""``.
    """
    cleaned = (path or "").strip().lstrip("/")
    if not cleaned:
        return ""
    parts = cleaned.split("/")
    if any(part in ("", ".", "..") for part in parts):
        raise GitHubInvalidError("Invalid path.")
    return "/".join(parts)


def get_github_client(user=None) -> GitHubClient:
    """Return an authenticated client for ``user`` (default: current user).

    Raises :class:`GitHubNotConnectedError` when the user has not connected a
    GitHub account, and :class:`GitHubAuthError` when the stored token cannot
    be decrypted.
    """
    from flask_login import current_user

    owner = user or current_user
    account = GithubAccount.query.filter_by(user_id=owner.id).first()
    if account is None:
        raise GitHubNotConnectedError("Connect your GitHub account to use this feature.")
    try:
        token = decrypt_secret(account.access_token_encrypted)
    except ValueError as exc:
        raise GitHubAuthError(str(exc)) from exc
    return GitHubClient(token)
