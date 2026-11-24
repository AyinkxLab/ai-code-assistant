"""The security advisory intake page keeps its promises honest (#625)."""

from __future__ import annotations

import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
POLICY = ROOT / "docs" / "security-advisory-intake.md"
SECURITY = ROOT / "SECURITY.md"
README = ROOT / "README.md"

_LINK_RE = re.compile(r"\[[^\]]*\]\(([^)]+)\)")
_SCHEME_RE = re.compile(r"^[a-zA-Z][a-zA-Z0-9+.-]*:")


def _text(path: Path) -> str:
    return path.read_text(encoding="utf-8")


def test_policy_is_linked_from_security_md_and_readme():
    assert "docs/security-advisory-intake.md" in _text(SECURITY)
    assert "docs/security-advisory-intake.md" in _text(README)


def test_policy_relative_links_resolve():
    missing = []
    for raw in _LINK_RE.findall(_text(POLICY)):
        raw = raw.strip()
        if not raw or raw.startswith("#") or _SCHEME_RE.match(raw):
            continue
        target = raw.split("#", 1)[0].split(" ", 1)[0].strip()
        if target and not (POLICY.parent / target).resolve().exists():
            missing.append(target)
    assert not missing, f"security advisory page has broken links: {missing}"


def test_policy_documents_private_intake_channels():
    text = _text(POLICY)

    assert "Report a vulnerability" in text
    assert "New draft security advisory" in text
    assert "private message" in text
    assert "Do not move the\nreport to a public issue" in text


def test_policy_states_critical_and_high_targets():
    text = _text(POLICY)

    # Acknowledgement targets.
    assert "**2 business days**" in text
    assert "**5 business days**" in text

    # Assessment and fix targets, scoped to the documented severity rows.
    critical_row = next(line for line in text.splitlines() if line.startswith("| Critical |"))
    high_row = next(line for line in text.splitlines() if line.startswith("| High |"))
    assert "**5 business days**" in critical_row
    assert "**30 calendar days**" in critical_row
    assert "**10 business days**" in high_row
    assert "**90 calendar days**" in high_row

    # The page must say these are commitments, not warranties.
    assert "commitments the maintainers make" in text
    assert "not a warranty" in text


def test_policy_explains_disclosure_and_missed_targets():
    text = _text(POLICY)

    assert "GitHub Security Advisory" in text
    assert "coordinated\n  disclosure date" in text or "coordinated" in text
    assert "## What happens when a target is missed" in text
    assert "post a status update in the private advisory" in text
    assert "Do not move the\nreport to a public issue" in text


def test_security_md_does_not_promise_more_than_the_policy():
    security_text = _text(SECURITY)
    policy_text = _text(POLICY)

    assert "as soon as possible" in security_text
    assert "docs/security-advisory-intake.md" in security_text
    # The policy owns the calendar commitments; SECURITY.md links to it.
    assert "30 calendar days" not in security_text
    assert "30 calendar days" in policy_text
