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
import json
import logging
import re
import time
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
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

#: Default and maximum ``per_page`` for list endpoints (GitHub caps at 100).
PAGE_SIZE_DEFAULT = 50
PAGE_SIZE_MAX = 100

#: Matches one ``<url>; rel="name"`` entry of a GitHub ``Link`` header.
_LINK_RE = re.compile(r'<([^>]+)>\s*;\s*rel="([^"]+)"')


@dataclass(frozen=True)
class GitHubPage:
    """A single page of a GitHub list endpoint plus its navigation metadata.

    ``has_next`` / ``has_prev`` / ``total_pages`` are derived from the ``Link``
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
            # Re-issue through _request so failures become the typed errors the
            # rest of the app expects (auth, rate limit, not found, ...).
            self._request("GET", path, params=params)

        try:
            items = response.json()
        except ValueError:
            items = []
        if not isinstance(items, list):
            items = []

        links = parse_link_header(response.headers.get("Link", ""))
        has_next = "next" in links
        has_prev = "prev" in links
        total_pages = _page_number_from_url(links.get("last")) or (page if not has_next else None)
        return GitHubPage(
            items=items[:per_page],
            page=page,
            per_page=per_page,
            has_next=has_next,
            has_prev=has_prev,
            total_pages=total_pages,
        )

    # -- GitHub account -----------------------------------------------------

    def get_user(self) -> dict:
        return self._get("/user")

    def get_rate_limit(self) -> dict | None:
        """Return the caller's core rate-limit budget, or ``None`` if unknown.

        Uses GitHub's dedicated ``/rate_limit`` endpoint, which does not count
        against the caller's quota. The result is normalized to
        ``{"limit", "remaining", "reset", "used"}`` where ``reset`` is a Unix
        timestamp. Only numeric fields are returned: no token, headers, or raw
        GitHub response is ever surfaced (issue #77).
        """
        data = self._get("/rate_limit")
        if not isinstance(data, dict):
            return None
        resources = data.get("resources")
        core = (resources or {}).get("core") if isinstance(resources, dict) else None
        if not isinstance(core, dict):
            core = data.get("rate")
        if not isinstance(core, dict):
            return None
        remaining = core.get("remaining")
        reset = core.get("reset")
        if remaining is None or reset is None:
            return None
        limit = core.get("limit")
        return {
            "limit": limit,
            "remaining": remaining,
            "reset": reset,
            "used": core.get("used"),
        }

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

    def _graphql(self, query: str, variables: dict) -> dict:
        """Execute a GraphQL query against the GitHub GraphQL API."""
        url = f"{self.api_url}/graphql"
        try:
            response = self.session.request(
                "POST",
                url,
                json={"query": query, "variables": variables},
                timeout=self.timeout,
            )
        except requests.RequestException as exc:
            raise GitHubNetworkError(
                "Could not reach the GitHub API. Please try again.", detail=str(exc)
            ) from exc
        if response.status_code == 401:
            raise GitHubAuthError(
                "Your GitHub connection is no longer valid. Reconnect your account."
            )
        if response.status_code >= 400:
            raise GitHubError(
                "The GitHub request failed. Please try again.",
                detail=_parse_error_body(response),
            )
        try:
            payload = response.json()
        except ValueError as exc:
            raise GitHubError("GitHub API returned an unexpected response.") from exc
        if payload.get("errors"):
            raise GitHubError("The GitHub request failed. Please try again.")
        return payload.get("data") or {}

    def get_last_commits(self, full_name: str, paths: list[str], ref: str) -> dict[str, dict]:
        """Return the last commit touching each path in a single request.

        GitHub's REST API can only answer "last commit for this path" one path
        at a time. Batching the per-path history lookups into one GraphQL query
        keeps this to a single API call regardless of how many files are shown.
        Files whose history cannot be resolved are simply omitted from the
        result.
        """
        if not paths:
            return {}
        owner, _, name = full_name.partition("/")
        fields = []
        for index, path in enumerate(paths):
            literal = json.dumps(path)  # safe GraphQL string literal
            fields.append(
                f"f{index}: object(expression: $ref) {{ ... on Commit {{ "
                f"history(first: 1, path: {literal}) {{ nodes {{ oid messageHeadline "
                f"committedDate author {{ name }} }} }} }} }}"
            )
        query = (
            "query($owner: String!, $name: String!, $ref: String!) { "
            "repository(owner: $owner, name: $name) {" + "\n".join(fields) + "}}"
        )
        data = self._graphql(query, {"owner": owner, "name": name, "ref": ref})
        repository = data.get("repository") or {}
        commits: dict[str, dict] = {}
        for index, path in enumerate(paths):
            node = repository.get(f"f{index}") or {}
            nodes = (node.get("history") or {}).get("nodes") or []
            if not nodes:
                continue
            commit = nodes[0]
            commits[path] = {
                "sha": commit.get("oid"),
                "message": commit.get("messageHeadline"),
                "author": (commit.get("author") or {}).get("name"),
                "date": commit.get("committedDate"),
            }
        return commits

    # -- Issues -------------------------------------------------------------

    def list_issues(self, full_name: str, *, state: str = "open", per_page: int = 50) -> list[dict]:
        """Return issues (excluding pull requests)."""
        data = self._get(
            f"/repos/{full_name}/issues",
            params={"state": state, "per_page": per_page},
        )
        return issues_only(data)

    def list_issues_page(
        self,
        full_name: str,
        *,
        state: str = "open",
        page: int = 1,
        per_page: int = PAGE_SIZE_DEFAULT,
    ) -> GitHubPage:
        """Return one page of issues (excluding pull requests).

        GitHub's issues endpoint includes pull requests, so the payload is
        filtered; the returned navigation metadata still reflects GitHub's own
        paging links for the endpoint.
        """
        result = self._get_page(
            f"/repos/{full_name}/issues",
            params={"state": state},
            page=page,
            per_page=per_page,
        )
        return GitHubPage(
            items=issues_only(result.items),
            page=result.page,
            per_page=result.per_page,
            has_next=result.has_next,
            has_prev=result.has_prev,
            total_pages=result.total_pages,
        )

    def get_issue(self, full_name: str, number: int) -> dict:
        return self._get(f"/repos/{full_name}/issues/{number}")

    # -- Pull requests ------------------------------------------------------

    def list_pull_requests(self, full_name: str, *, state: str = "open") -> list[dict]:
        return self._get(
            f"/repos/{full_name}/pulls",
            params={"state": state, "per_page": 50},
        )

    def list_pull_requests_page(
        self,
        full_name: str,
        *,
        state: str = "open",
        page: int = 1,
        per_page: int = PAGE_SIZE_DEFAULT,
    ) -> GitHubPage:
        """Return one page of pull requests with Link-header navigation."""
        return self._get_page(
            f"/repos/{full_name}/pulls",
            params={"state": state},
            page=page,
            per_page=per_page,
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


def _license_label(license_data: dict | None) -> str | None:
    """Return a short license label, preferring the SPDX id."""
    if not license_data:
        return None
    spdx = license_data.get("spdx_id")
    if spdx and spdx != "NOASSERTION":
        return spdx
    return license_data.get("name")


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
        "stars": repo.get("stargazers_count") or 0,
        "forks": repo.get("forks_count") or 0,
        "open_issues_count": repo.get("open_issues_count") or 0,
        "license": _license_label(repo.get("license")),
        "topics": repo.get("topics") or [],
        "homepage": repo.get("homepage") or "",
    }


def issues_only(items: list[dict]) -> list[dict]:
    """Return only true issues from a GitHub issues payload (drop PRs)."""
    return [item for item in items if "pull_request" not in item]


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


def revoke_github_token(access_token: str) -> None:
    """Best-effort revocation of a GitHub OAuth token."""
    try:
        response = requests.delete(
            GITHUB_REVOKE_URL.format(client_id=_github_config("GITHUB_CLIENT_ID")),
            auth=(_github_config("GITHUB_CLIENT_ID"), _github_config("GITHUB_CLIENT_SECRET")),
            json={"access_token": access_token},
            headers={"Accept": "application/vnd.github+json"},
            timeout=_github_config("GITHUB_REQUEST_TIMEOUT"),
        )
        if response.status_code >= 400:
            logger.warning("GitHub token revocation failed with HTTP %s", response.status_code)
    except requests.RequestException as exc:
        logger.warning("GitHub token revocation failed: %s", exc)


def _refresh_github_token(refresh_token: str) -> dict | None:
    """Exchange a GitHub refresh token for a new token pair."""
    try:
        response = requests.post(
            GITHUB_TOKEN_URL,
            headers={"Accept": "application/json"},
            data={
                "client_id": _github_config("GITHUB_CLIENT_ID"),
                "client_secret": _github_config("GITHUB_CLIENT_SECRET"),
                "grant_type": "refresh_token",
                "refresh_token": refresh_token,
            },
            timeout=_github_config("GITHUB_REQUEST_TIMEOUT"),
        )
        data = response.json()
    except (requests.RequestException, ValueError):
        return None
    if response.status_code >= 400 or "access_token" not in data:
        return None
    return data


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
        refresh_token = (
            decrypt_secret(account.refresh_token_encrypted)
            if account.refresh_token_encrypted
            else None
        )
    except ValueError as exc:
        raise GitHubAuthError(
            "Your GitHub connection is no longer valid. Reconnect your account.",
            detail=str(exc),
        ) from exc
    expires_at = account.token_expires_at
    if expires_at and expires_at.tzinfo is None:
        expires_at = expires_at.replace(tzinfo=UTC)
    if expires_at and expires_at <= datetime.now(UTC):
        refreshed = _refresh_github_token(refresh_token) if refresh_token else None
        if refreshed is None:
            db.session.delete(account)
            db.session.commit()
            raise GitHubNotConnectedError("Connect your GitHub account to use this feature.")
        account.set_access_token(refreshed["access_token"])
        if "refresh_token" in refreshed:
            account.set_refresh_token(refreshed["refresh_token"])
        account.token_expires_at = (
            datetime.now(UTC) + timedelta(seconds=int(refreshed["expires_in"]))
            if refreshed.get("expires_in")
            else None
        )
        db.session.commit()
        token = refreshed["access_token"]
    return GitHubClient(token)
