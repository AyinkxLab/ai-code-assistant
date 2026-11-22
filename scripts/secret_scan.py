#!/usr/bin/env python3
"""Scan the working tree for accidentally committed credentials (#624).

This is a deterministic, dependency-free complement to GitHub secret scanning.
It catches common credential shapes and high-entropy values assigned to
secret-like names. It is a lint, not a proof: a determined attacker can encode a
secret in a way these patterns do not recognise.

Patterns
--------
``aws_access_key``          ``AKIA``/``ASIA`` followed by 16 uppercase/digits
``github_token``            ``ghp_``/``gho_``/``ghu_``/``ghs_``/``ghr_`` or ``github_pat_``
``gitlab_token``            ``glpat-`` prefixed tokens
``slack_token``             ``xoxb-``/``xoxp-``/``xoxa-``/``xoxr-``/``xoxs-`` tokens
``stripe_live_key``         ``sk_live_``/``rk_live_`` keys
``openai_key``              ``sk-``/``sk-proj-`` keys of at least 20 characters
``private_key``             PEM private-key header
``generic_secret_assignment``
    A quoted 20+ character value assigned to a secret-like name, when the value
    is not a placeholder and has Shannon entropy >= 3.5.
``github_pat_`` values are covered by ``github_token``.

Allowlist
---------
``.github/secret-scan-allowlist.json``::

    {"version": 1, "allow": [
      {"pattern": "generic_secret_assignment", "value": "synthetic-value",
       "reason": "test fixture"}
    ]}

An allowlist entry suppresses a finding only when both the pattern name and the
exact matched value match. Values are never printed in full.

Exit codes
----------
0   no findings
1   at least one finding
2   invalid input, unreadable path, or malformed allowlist

Run ``python scripts/secret_scan.py .`` in CI or locally.
"""

from __future__ import annotations

import argparse
import json
import math
import re
import sys
from pathlib import Path

EXIT_CLEAN = 0
EXIT_FINDINGS = 1
EXIT_ERROR = 2

DEFAULT_ALLOWLIST = Path(".github") / "secret-scan-allowlist.json"
MAX_FILE_BYTES = 2 * 1024 * 1024
MAX_FINDINGS = 1000

SKIP_DIRS = {
    ".git",
    ".hg",
    ".svn",
    ".venv",
    "venv",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    "dist",
    "build",
}
SKIP_DIR_PREFIXES = (".venv",)

#: Pattern name -> compiled expression. The value is group 0 unless a named
#: ``value`` group is present.
PATTERNS: dict[str, re.Pattern[str]] = {
    "aws_access_key": re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    "github_token": re.compile(
        r"\b(?:gh[pousr]_[A-Za-z0-9]{36,255}|github_pat_[A-Za-z0-9_]{22,255})\b"
    ),
    "gitlab_token": re.compile(r"\bglpat-[A-Za-z0-9_-]{20,}\b"),
    "slack_token": re.compile(r"\bxox[baprs]-[A-Za-z0-9-]{10,}\b"),
    "stripe_live_key": re.compile(r"\b(?:sk|rk)_live_[A-Za-z0-9]{16,}\b"),
    "openai_key": re.compile(r"\bsk-(?:proj-)?[A-Za-z0-9_-]{20,}\b"),
    "private_key": re.compile(r"-----BEGIN (?:RSA |EC |OPENSSH |DSA |PGP )?PRIVATE KEY-----"),
    "generic_secret_assignment": re.compile(
        r"(?i)\b(?:api[_-]?key|apikey|secret|client[_-]?secret|"
        r"access[_-]?token|auth[_-]?token|refresh[_-]?token|password|passwd|"
        r"private[_-]?key)\b\s*[:=]\s*[\"'](?P<value>[^\"'\s]{20,})[\"']"
    ),
}

#: Values containing these markers are placeholders, not credentials.
_PLACEHOLDER_MARKERS = (
    "example",
    "changeme",
    "change-me",
    "placeholder",
    "dummy",
    "sample",
    "test",
    "your-",
    "your_",
    "xxxx",
    "redacted",
    "not-a-",
    "supersecret",
    "password",
    "secret",
    "token",
    "value",
)


class SecretScanError(Exception):
    """A configuration or input error (exit status 2)."""


def _entropy(value: str) -> float:
    if not value:
        return 0.0
    counts = {}
    for char in value:
        counts[char] = counts.get(char, 0) + 1
    length = len(value)
    return -sum((count / length) * math.log2(count / length) for count in counts.values())


def _looks_like_placeholder(value: str) -> bool:
    lowered = value.lower()
    return any(marker in lowered for marker in _PLACEHOLDER_MARKERS)


def _redact(value: str) -> str:
    """Return a short, non-reusable fragment for reports."""
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-2:]}"


def load_allowlist(path: Path, *, required: bool = False) -> list[dict]:
    """Load and validate the allowlist. A missing optional file allows nothing."""
    if not path.exists():
        if required:
            raise SecretScanError(f"allowlist not found: {path}")
        return []
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SecretScanError(f"invalid allowlist {path}: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("allow", []), list):
        raise SecretScanError("allowlist must be a JSON object with an 'allow' list")
    entries = []
    for entry in data.get("allow", []):
        if (
            not isinstance(entry, dict)
            or not isinstance(entry.get("pattern"), str)
            or not isinstance(entry.get("value"), str)
            or not entry.get("value")
        ):
            raise SecretScanError("each allowlist entry needs string 'pattern' and 'value'")
        entries.append(entry)
    return entries


def _allowed(finding: dict, allowlist: list[dict]) -> bool:
    return any(
        entry["pattern"] == finding["pattern"] and entry["value"] == finding["value"]
        for entry in allowlist
    )


def scan_text(
    text: str, path: str = "<text>", *, allowlist: list[dict] | None = None
) -> list[dict]:
    """Return findings for ``text``. Values are included for allowlist matching."""
    allowlist = allowlist or []
    findings: list[dict] = []
    for lineno, line in enumerate(text.splitlines(), start=1):
        for name, pattern in PATTERNS.items():
            for match in pattern.finditer(line):
                value = match.groupdict().get("value") or match.group(0)
                if name == "generic_secret_assignment" and (
                    _looks_like_placeholder(value) or _entropy(value) < 3.5
                ):
                    continue
                finding = {
                    "path": path,
                    "line": lineno,
                    "pattern": name,
                    "value": value,
                    "snippet": line.strip()[:200],
                }
                if not _allowed(finding, allowlist):
                    findings.append(finding)
    return findings


def _skip_dir(name: str) -> bool:
    return name in SKIP_DIRS or name.startswith(SKIP_DIR_PREFIXES)


def _iter_files(paths: list[Path]):
    for path in paths:
        if path.is_dir():
            for candidate in sorted(path.rglob("*")):
                if candidate.is_file() and not any(
                    _skip_dir(part) for part in candidate.relative_to(path).parts[:-1]
                ):
                    yield candidate
        elif path.is_file():
            yield path
        else:
            raise SecretScanError(f"path not found: {path}")


def scan_paths(paths: list[Path], *, allowlist: list[dict] | None = None) -> list[dict]:
    """Scan files/directories, skipping binary and oversized files."""
    allowlist = allowlist or []
    findings: list[dict] = []
    for path in _iter_files(paths):
        try:
            raw = path.read_bytes()
        except OSError as exc:
            raise SecretScanError(f"cannot read {path}: {exc}") from exc
        if len(raw) > MAX_FILE_BYTES or b"\0" in raw[:2048]:
            continue
        text = raw.decode("utf-8", errors="replace")
        findings.extend(scan_text(text, str(path), allowlist=allowlist))
        if len(findings) >= MAX_FINDINGS:
            return findings[:MAX_FINDINGS]
    return findings


def _public_finding(finding: dict) -> dict:
    return {
        "path": finding["path"],
        "line": finding["line"],
        "pattern": finding["pattern"],
        "snippet": _redact(finding["value"]),
    }


def _emit_error(message: str, as_json: bool) -> None:
    if as_json:
        print(json.dumps({"error": message, "code": "invalid_input"}))
    else:
        print(message, file=sys.stderr)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Scan for committed credentials.")
    parser.add_argument("paths", nargs="*", default=["."], help="Files or directories.")
    parser.add_argument("--allowlist", type=Path, default=None, help="Allowlist JSON.")
    parser.add_argument("--json", dest="as_json", action="store_true", help="JSON output.")
    args = parser.parse_args(argv)

    allowlist_path = args.allowlist
    allowlist = []
    if allowlist_path is not None:
        try:
            allowlist = load_allowlist(allowlist_path, required=True)
        except SecretScanError as exc:
            _emit_error(str(exc), args.as_json)
            return EXIT_ERROR
    else:
        try:
            allowlist = load_allowlist(DEFAULT_ALLOWLIST)
        except SecretScanError as exc:
            _emit_error(str(exc), args.as_json)
            return EXIT_ERROR

    try:
        findings = scan_paths([Path(value) for value in args.paths], allowlist=allowlist)
    except SecretScanError as exc:
        _emit_error(str(exc), args.as_json)
        return EXIT_ERROR

    public = [_public_finding(finding) for finding in findings]
    if args.as_json:
        print(
            json.dumps(
                {
                    "ok": not findings,
                    "count": len(public),
                    "truncated": len(findings) >= MAX_FINDINGS,
                    "findings": public,
                },
                sort_keys=True,
            )
        )
    else:
        for finding in public:
            print(
                f"{finding['path']}:{finding['line']}: "
                f"{finding['pattern']}: {finding['snippet']}"
            )
        if findings:
            print(f"{len(public)} potential credential(s) found.", file=sys.stderr)
        else:
            print("No credentials found.")

    return EXIT_FINDINGS if findings else EXIT_CLEAN


if __name__ == "__main__":
    sys.exit(main())
