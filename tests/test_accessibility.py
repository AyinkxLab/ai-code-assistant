"""Accessibility contract for the server-rendered keyboard UI (#713)."""

from __future__ import annotations

import re
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
TEMPLATES = REPO_ROOT / "app" / "templates"
STYLE = REPO_ROOT / "app" / "static" / "css" / "style.css"


def test_page_starts_with_skip_link_and_main_landmark(client):
    html = client.get("/").get_data(as_text=True)

    assert '<a class="skip-link" href="#main-content">Skip to main content</a>' in html
    assert '<main id="main-content"' in html
    assert html.index('class="skip-link"') < html.index("<header")


def test_navigation_is_a_labelled_landmark(client):
    html = client.get("/").get_data(as_text=True)
    assert '<nav class="nav" aria-label="Primary">' in html


def test_project_scripts_load_the_focus_manager(client):
    html = client.get("/").get_data(as_text=True)
    assert "js/a11y.js" in html


def test_workspace_dialog_exposes_modal_semantics():
    template = (TEMPLATES / "workspaces" / "index.html").read_text(encoding="utf-8")

    assert 'id="workspace-modal" class="modal" role="dialog"' in template
    assert 'aria-modal="true"' in template
    assert 'aria-labelledby="workspace-modal-title"' in template
    assert '<h2 id="workspace-modal-title">New workspace</h2>' in template


def test_file_dialog_exposes_modal_semantics():
    template = (TEMPLATES / "github" / "repo_detail.html").read_text(encoding="utf-8")

    assert 'id="file-modal" class="modal" role="dialog"' in template
    assert 'aria-modal="true"' in template
    assert 'aria-labelledby="file-modal-title"' in template
    assert '<h3 id="file-modal-title">File</h3>' in template


def test_templates_do_not_use_positive_tabindex():
    offenders = []
    for template in TEMPLATES.rglob("*.html"):
        for number, line in enumerate(template.read_text(encoding="utf-8").splitlines(), 1):
            if re.search(r'tabindex="[1-9]', line):
                offenders.append(f"{template.relative_to(REPO_ROOT)}:{number}")
    assert offenders == []


def test_keyboard_focus_styles_exist():
    css = STYLE.read_text(encoding="utf-8")

    assert ":focus-visible" in css
    assert "outline: 2px solid var(--accent)" in css
    # The flex modal must not stay on screen when the hidden attribute is set.
    assert ".modal[hidden]" in css
    assert "display: none" in css


def test_accessibility_guide_is_linked_from_readme():
    readme = (REPO_ROOT / "README.md").read_text(encoding="utf-8")
    assert "docs/accessibility.md" in readme
