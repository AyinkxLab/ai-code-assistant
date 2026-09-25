"""Map file paths and fenced-code tags to highlight.js language ids (issue #44).

Keeping this mapping server-side (and unit-tested) lets the client highlight
only languages highlight.js is known to support, and fail gracefully for
anything unknown. The client-side glue (``static/js/highlight.js``) is a no-op
when a block has no recognised language.
"""

from __future__ import annotations

#: File extension -> highlight.js language id.
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

_KNOWN_LANGUAGE_IDS = set(EXTENSION_LANGUAGES.values()) | set(LANGUAGE_ALIASES.values())


def normalize_language(token: str | None) -> str | None:
    """Return the highlight.js id for a fence tag, or ``None`` when unknown."""
    value = (token or "").strip().lower()
    if not value:
        return None
    if value in LANGUAGE_ALIASES:
        return LANGUAGE_ALIASES[value]
    if value in _KNOWN_LANGUAGE_IDS:
        return value
    return EXTENSION_LANGUAGES.get(value)


def language_for_path(path: str | None) -> str | None:
    """Return the highlight.js id for a file path, or ``None`` when unknown."""
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
