"""Syntax-highlighting language helper (issue #44)."""

import pathlib

from app.services.highlighting import (
    EXTENSION_LANGUAGES,
    highlight_class,
    language_for_path,
    normalize_language,
)


class TestNormalizeLanguage:
    def test_maps_known_fence_tags(self):
        assert normalize_language("python") == "python"
        assert normalize_language("rust") == "rust"
        assert normalize_language("typescript") == "typescript"

    def test_resolves_aliases(self):
        assert normalize_language("py") == "python"
        assert normalize_language("JS") == "javascript"
        assert normalize_language("C++") == "cpp"
        assert normalize_language("sh") == "bash"

    def test_unknown_language_is_none(self):
        assert normalize_language("brainfuck") is None
        assert normalize_language("") is None
        assert normalize_language(None) is None
        assert normalize_language("   ") is None


class TestLanguageForPath:
    def test_maps_extensions(self):
        assert language_for_path("src/app.py") == "python"
        assert language_for_path("Makefile") == "makefile"
        assert language_for_path("infra/main.tf") == "hcl"
        assert language_for_path("Dockerfile") == "dockerfile"

    def test_handles_backslashes_and_case(self):
        assert language_for_path("SRC\\App.TSX") == "typescript"

    def test_unknown_extension_is_none(self):
        assert language_for_path("data.unknownext") is None
        assert language_for_path("README") is None
        assert language_for_path(None) is None
        assert language_for_path("") is None


class TestHighlightClass:
    def test_known_language_yields_class(self):
        assert highlight_class("python") == "language-python"
        assert highlight_class("py") == "language-python"

    def test_unknown_language_yields_empty_string(self):
        assert highlight_class("not-a-language") == ""
        assert highlight_class(None) == ""

    def test_every_mapped_language_is_a_nonempty_id(self):
        for language in set(EXTENSION_LANGUAGES.values()):
            assert highlight_class(language) == f"language-{language}"


class TestHighlightingIsWiredLocally:
    def test_vendored_library_is_present_and_nonempty(self, app):
        vendor = pathlib.Path(app.root_path) / "static" / "vendor" / "highlight.min.js"
        assert vendor.exists()
        assert vendor.stat().st_size > 1000

    def test_base_template_serves_highlighter_locally(self, app):
        base = (pathlib.Path(app.root_path) / "templates" / "base.html").read_text(encoding="utf-8")
        assert "vendor/highlight.min.js" in base
        assert "js/highlight.js" in base
        assert "css/highlight-dark.css" in base
        # No CDN dependency.
        assert "cdn.jsdelivr.net" not in base
        assert "unpkg.com" not in base
