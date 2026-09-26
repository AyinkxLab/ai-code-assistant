"""Syntax-highlighting language helper (issue #44)."""

import pathlib

from app.services.highlighting import (
    EXTENSION_LANGUAGES,
    LANGUAGE_ALIASES,
    SPECIAL_FILENAMES,
    highlight_class,
    language_for_path,
    normalize_language,
)

#: Language ids the server can emit for a file, i.e. everything the client may
#: be asked to highlight.
SERVER_LANGUAGE_IDS = (
    set(EXTENSION_LANGUAGES.values())
    | set(LANGUAGE_ALIASES.values())
    | set(SPECIAL_FILENAMES.values())
)

#: Language ids the vendored highlight.js build cannot ship a grammar for; the
#: client-side tokenizer in ``static/js/highlight.js`` covers these instead.
CLIENT_FALLBACK_LANGUAGES = {"solidity", "hcl", "graphql"}


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

    def test_file_viewers_use_the_shared_highlighter(self, app):
        scripts = pathlib.Path(app.root_path) / "static" / "js"
        for name in ("project.js", "chat_files.js"):
            source = (scripts / name).read_text(encoding="utf-8")
            assert "AICASyntaxHighlight" in source


class TestVendoredGrammarCoverage:
    """The vendored build must cover every language the server can store (#94)."""

    def _vendor(self, app) -> str:
        return (pathlib.Path(app.root_path) / "static" / "vendor" / "highlight.min.js").read_text(
            encoding="utf-8"
        )

    def _glue(self, app) -> str:
        return (pathlib.Path(app.root_path) / "static" / "js" / "highlight.js").read_text(
            encoding="utf-8"
        )

    def test_vendored_bundle_registers_every_server_language(self, app):
        vendor = self._vendor(app)
        missing = sorted(
            language
            for language in SERVER_LANGUAGE_IDS - CLIENT_FALLBACK_LANGUAGES
            if f'"{language}"' not in vendor
        )
        assert missing == []

    def test_client_tokenizer_covers_the_remaining_languages(self, app):
        glue = self._glue(app)
        for language in CLIENT_FALLBACK_LANGUAGES:
            assert language in glue

    def test_client_guard_skips_oversized_blocks(self, app):
        assert "MAX_HIGHLIGHT_CHARS" in self._glue(app)
