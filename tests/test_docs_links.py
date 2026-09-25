"""Link checks for repository documentation (#200).

The Soroban workflow guide (and the docs it links to) must not rot. This walks
every top-level and ``docs/`` markdown file, extracts inline links, and asserts
that relative targets resolve to real files. External ``http(s)``/``mailto``
links and same-page anchors are ignored so the check stays offline and
deterministic.
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _markdown_files() -> list[Path]:
    return sorted(REPO_ROOT.glob("*.md")) + sorted((REPO_ROOT / "docs").glob("*.md"))


def _relative_link_targets(path: Path) -> list[str]:
    """Return the relative (non-external, non-anchor) link targets in ``path``."""
    targets: list[str] = []
    for match in _LINK_RE.finditer(path.read_text(encoding="utf-8")):
        raw = match.group(1).strip()
        if not raw or raw.startswith(("#", "//")) or _SCHEME_RE.match(raw):
            continue
        # Drop a trailing #anchor and an optional "title".
        target = raw.split("#", 1)[0].split(" ", 1)[0].strip()
        if target:
            targets.append(target)
    return targets


@pytest.mark.parametrize("doc_path", _markdown_files(), ids=lambda p: str(p.relative_to(REPO_ROOT)))
def test_relative_doc_links_resolve(doc_path: Path):
    missing = [
        target
        for target in _relative_link_targets(doc_path)
        if not (doc_path.parent / target).resolve().exists()
    ]
    assert not missing, f"{doc_path.relative_to(REPO_ROOT)} has broken links: {missing}"


def test_soroban_workflow_guide_is_linked_from_readme():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/soroban-workflow.md" in readme
