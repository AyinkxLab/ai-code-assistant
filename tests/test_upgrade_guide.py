"""Release notes and upgrade guide stay complete (#644)."""

from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def test_changelog_links_the_upgrade_guide():
    changelog = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "## [Unreleased]" in changelog
    assert "upgrade-guide.md" in changelog


def test_upgrade_guide_documents_the_required_sections():
    guide = (ROOT / "docs/upgrade-guide.md").read_text(encoding="utf-8")

    for heading in (
        "## Schema changes",
        "## New settings",
        "## Required operator actions",
        "## Rollback notes",
    ):
        assert heading in guide
