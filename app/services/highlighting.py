"""Map file paths and fenced-code tags to highlight.js language ids (issue #44).

Keeping this mapping server-side (and unit-tested) lets the client highlight
only languages highlight.js is known to support, and fail gracefully for
anything unknown. The client-side glue (**static/js/highlight.js**) is a no-op
when a block has no recognised language.

This module also exposes the escaping helper used to highlight search
matches inside the file viewer (issue #117). Highlighting is done on a
copy of the stored content, never mutating it, and all inserted markup is
escaped so a search term cannot inject HTML.
"""

from __future__ import annotations

import re
from typing import Iterable

#/: File extension -> highlight.js language id.
EXTENSION_LANGUAGES: dict[str, str] = {
    "py": "python",
    "pyi": "python",
    "js": "javascript",
    "mjs": "javascript",
    "cjs": "javascript",
    "jsx": "javascript",
    "ts": "typescript",
    "tsx": "typescript",
    "rs": "rust",
    "go": "go",
    "java": "java",
    "kt": "kotlin",
    "kts": "kotlin",
    "swift": "swift",
    "dart": "dart",
    "rb": "ruby",
    "php": "php",
    "c": "c",
    "h": "c",
    "cpp": "cpp",
    "cc": "cpp",
    "cxx": "cpp",
    "hpp": "cpp",
    "cs": "csharp",
    "sh": "bash",
    "bash": "bash",
    "zsh": "bash",
    "fish": "bash",
    "ps1": "powershell",
    "sql": "sql",
    "json": "json",
    "yaml": "yaml",
    "yml": "yaml",
    "toml": "ini",
    "ini": "ini",
    "cfg": "ini",
    "html": "xml",
    "htm": "xml",
    "xml": "xml",
    "css": "css",
    "scss": "scss",
    "less": "less",
    "md": "markdown",
    "markdown": "markdown",
    "rst": "markdown",
    "sol": "solidity",
    "ex": "elixir",
    "exs": "elixir",
    "erl": "erlang",
    "lua": "lua",
    "r": "r",
    "pl": "perl",
    "pm": "perl",
    "tf": "hcl",
    "hcl": "hcl",
    "graphql": "graphql",
    "gql": "graphql",
    "proto": "protobuf",
    "vim": "vim",
    "asm": "x86asm",
}

#: Fenced-code tag aliases that are not valid highlight.js ids verbatim.
LANGUAGE_ALIASES: dict[str, str] = {
    "py": "python",
    "python3": "python",
    "python2": "python",
    "js": "javascript",
    "node": "javascript",
    "nodejs": "javascript",
    "ts": "typescript",
    "rs": "rust",
    "sh": "bash",
    "shell": "bash",
    "console": "bash",
    "yml": "yaml",
    "c++": "cpp",
    "cxx": "cpp",
    "c#": "csharp",
    "cs": "csharp",
    "htm": "xml",
    "html": "xml",
    "docker": "dockerfile",
}

#: Extensionless files that still have a known language.
SPECIAL_FILENAMES: dict[str, str] = {
    "dockerfile": "dockerfile",
    "containerfile": "dockerfile",
    "makefile": "makefile",
    "gemfile": "ruby",
    "rakefile": "ruby",
    ".gitignore": "bash",
}

KNOWN_LANGUAGE_IDS = set(EXTENSION_LANGUAGES.values()) | set(LANGUAGE_ALIASES.values())


def normalize_language(token: str | None) -> str | None:
    """Return the highlight.js id for a fence tag, or `None` when unknown."""
    value = (token or "").strip().lower()
    if not value:
        return None
    if value in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[value]
    if value in KNOWN_LANGUAGE_IDs:
        return value
    return EXTENSION_LANGUAGES.get(value)


def language_for_path(path: str | None) -> str | None:
    """Return the highlight.js id for a file path, or `None` when unknown."""
    if not path:
        return None
    name = str(path).replace("\\", "/").rsplit("/", 1)[-1].lower()
    special = SPECIAL_FILENAMES.get(name)
    if special:
        return special
    if "." in name:
        return EXTENSION_LANGUAGES.get(name.rsplit(".", 1)[-1])
    return None


def highlight_class(token: str | None) -> str:
    """Return the CSS class for a language token, or ``""`` when unknown.

    Unknown languages intentionally yield an empty string so callers render the
    block unhighlighted rather than guessing (issue #44 acceptance criterion).
    """
    language = normalize_language(token)
    return f"language-{language}" if language else ""


def _escape_html(text: str) -> str:
    """Escape `&`, `<`, `>`, `"`, and `'` for safe HTML interpolation."""
    return (
        str(text)
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
        .replace("'", "&#39;")
    )


def highlight_matches(content: str, terms: Iterable[str] | str | None, case_sensitive: bool = False) -> str:
    """Return HTML-escaped content with every occurrence of each term wrapped in a mark.

    The stored content is never mutated: this function operates on an
    escaped copy and only adds <mark class="search-hit"> spans around matches.
    Terms are matched literally (regex metacharacters are escaped), and the
    escaped markup is never re-scanned, so a term cannot inject HTML.
    """
    if content is None:
        return ""
    if terms is None:
        return _escape_html(content)
    if isinstance(terms, str):
        term_list = [terms]
    else:
        term_list = [t for t in terms if isinstance(t, str)]

    cleaned = []
    seen = set()
    for term in term_list:
        if not term:
            continue
        key = term if case_sensitive else term.lower()
        if key in seen:
            continue
        seen.add(key)
        cleaned.append(term)

    if not cleaned:
        return _escape_html(content)

    flags = 0 if case_sensitive else re.IGNORECASE
    pattern = re.compile("|".join(re.escape(t) for t in cleaned), flags)

    out = []
    last = 0
    for match in pattern.finditer(content):
        out.append(_escape_html(content[last:match.start()]))
        out.append('<mark class="search-hit">')
        out.append(_escape_html(match.group(0)))
        out.append("</mark>")
        last = match.end()
    out.append(_escape_html(content[last:]))
    return "".join(out)
