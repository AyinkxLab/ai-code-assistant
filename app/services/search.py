"""Project-wide search service.

Searches an indexed project by file path and/or file contents, with optional
filters for language, scope (path-only / content-only), and regular-expression
mode. Results are bounded so a single query never returns an unbounded number
of matches, and binary or content-less files are skipped for content matches.

The literal (default) mode escapes LIKE wildcards so user input matches
literally; regex mode compiles the pattern safely (an invalid pattern raises
:class:`SearchQueryError`, which the route maps to a 400) and applies Python's
``re`` engine while still respecting the result limit.
"""

from __future__ import annotations

import re

from flask import current_app
from sqlalchemy import func

from app.extensions import db
from app.models import ProjectFile

_SNIPPET_RADIUS = 80
_SCOPES = ("all", "path", "content")
# Upper bound on files scanned in regex mode. Literal mode is bounded by the
# database query limit; regex needs Python-side scanning, so a hard scan cap
# keeps the work bounded even when few files match.
_MAX_REGEX_SCAN = 5000


class SearchQueryError(ValueError):
    """Raised for an invalid search query (e.g. a malformed regular expression)."""


def _escape_like(value: str) -> str:
    """Escape LIKE wildcards so user input is matched literally."""
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _snippet_span(text: str, start: int, end: int) -> str:
    """Return a short snippet surrounding the ``[start, end)`` match."""
    start = max(0, start - _SNIPPET_RADIUS)
    end = min(len(text), end + _SNIPPET_RADIUS)
    prefix = "…" if start > 0 else ""
    suffix = "…" if end < len(text) else ""
    snippet = text[start:end].replace("\n", " ").replace("\r", "")
    return f"{prefix}{snippet}{suffix}"


def _snippet(text: str, needle: str) -> str | None:
    """Return a short snippet surrounding the first literal match of ``needle``."""
    index = text.lower().find(needle)
    if index < 0:
        return None
    return _snippet_span(text, index, index + len(needle))


def _compile_regex(query: str, case_sensitive: bool):
    """Compile ``query`` as a regex, raising :class:`SearchQueryError` if invalid."""
    flags = 0 if case_sensitive else re.IGNORECASE
    try:
        return re.compile(query, flags)
    except re.error as exc:
        raise SearchQueryError(f"Invalid regular expression: {exc}") from exc


def _normalize_limit(limit) -> int:
    max_results = current_app.config["PROJECT_SEARCH_MAX_RESULTS"]
    if limit is None:
        limit = max_results
    return max(1, min(int(limit), max_results))


def _base_criteria(project_id: int, language: str | None) -> list:
    criteria = [ProjectFile.project_id == project_id]
    if language:
        criteria.append(func.lower(ProjectFile.language) == language.lower())
    return criteria


def _row_result(file: ProjectFile, matched: str) -> dict:
    return {
        "path": file.path,
        "size": file.size,
        "language": file.language,
        "matched": matched,
    }


def _literal_search(
    project_id: int,
    query: str,
    *,
    case_sensitive: bool,
    limit: int,
    language: str | None,
    scope: str,
) -> list[dict]:
    needle = query if case_sensitive else query.lower()
    pattern = f"%{_escape_like(needle)}%"
    path_col = ProjectFile.path if case_sensitive else func.lower(ProjectFile.path)
    content_col = ProjectFile.content if case_sensitive else func.lower(ProjectFile.content)
    criteria = _base_criteria(project_id, language)

    def rows_for(*extra):
        return (
            db.session.query(ProjectFile)
            .filter(*criteria, *extra)
            .order_by(ProjectFile.path.asc())
            .limit(limit)
            .all()
        )

    path_rows = rows_for(path_col.like(pattern, escape="\\")) if scope in ("all", "path") else []
    content_rows = (
        rows_for(
            ProjectFile.is_binary.is_(False),
            ProjectFile.content.isnot(None),
            content_col.like(pattern, escape="\\"),
        )
        if scope in ("all", "content")
        else []
    )

    results: list[dict] = []
    seen: set[int] = set()
    combined = [
        *((f, "path") for f in path_rows),
        *((f, "content") for f in content_rows),
    ]
    for file, matched in combined:
        if len(results) >= limit:
            break
        if file.id in seen:
            continue
        seen.add(file.id)
        result = _row_result(file, matched)
        if matched == "content" and file.content:
            result["snippet"] = _snippet(file.content, needle)
        results.append(result)
    return results


def _regex_search(
    project_id: int,
    regex,
    *,
    limit: int,
    language: str | None,
    scope: str,
) -> list[dict]:
    criteria = _base_criteria(project_id, language)
    if scope == "content":
        criteria.append(ProjectFile.is_binary.is_(False))
        criteria.append(ProjectFile.content.isnot(None))
    candidates = (
        db.session.query(ProjectFile)
        .filter(*criteria)
        .order_by(ProjectFile.path.asc())
        .limit(_MAX_REGEX_SCAN)
        .all()
    )

    results: list[dict] = []
    for file in candidates:
        if len(results) >= limit:
            break
        if scope in ("all", "path") and regex.search(file.path):
            results.append(_row_result(file, "path"))
            continue
        if scope in ("all", "content") and not file.is_binary and file.content:
            match = regex.search(file.content)
            if match is not None:
                result = _row_result(file, "content")
                result["snippet"] = _snippet_span(file.content, match.start(), match.end())
                results.append(result)
    return results


def search_project(
    project_id: int,
    query: str,
    *,
    case_sensitive: bool = False,
    limit: int | None = None,
    language: str | None = None,
    scope: str = "all",
    regex: bool = False,
) -> dict:
    """Search ``project_id`` for ``query`` and return bounded results.

    Filters compose with the query string API: ``language`` restricts to files
    whose language matches (case-insensitive), ``scope`` is ``all``/``path``/
    ``content``, and ``regex`` switches from literal to regular-expression
    matching. Returns ``{"query", "total", "results", "language", "scope",
    "regex"}`` where each result is a file with ``path``, ``size``,
    ``language``, ``matched`` (``path`` or ``content``), and an optional
    ``snippet``. An invalid regex or scope raises :class:`SearchQueryError`.
    """
    query = (query or "").strip()
    scope = (scope or "all").strip().lower()
    language = (language or "").strip() or None
    empty = {
        "query": "",
        "total": 0,
        "results": [],
        "language": language,
        "scope": scope,
        "regex": bool(regex),
    }
    if not query:
        return empty
    if scope not in _SCOPES:
        raise SearchQueryError(f"Invalid scope: {scope!r}")
    limit = _normalize_limit(limit)
    if len(query) > 200:
        query = query[:200]

    if regex:
        results = _regex_search(
            project_id,
            _compile_regex(query, case_sensitive),
            limit=limit,
            language=language,
            scope=scope,
        )
    else:
        results = _literal_search(
            project_id,
            query,
            case_sensitive=case_sensitive,
            limit=limit,
            language=language,
            scope=scope,
        )

    return {
        "query": query,
        "total": len(results),
        "results": results,
        "language": language,
        "scope": scope,
        "regex": bool(regex),
    }
