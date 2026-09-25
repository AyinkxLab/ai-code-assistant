"""XSS-hardening tests for the chat Markdown renderer (issue #35).

The renderer lives in ``app/static/js/chat_markdown.js`` and is intentionally
DOM-free, so the checked-in script is evaluated under Node. No npm packages are
required; the tests skip only when a Node runtime is unavailable.
"""

import contextlib
import json
import os
import shutil
import subprocess
import tempfile
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "app" / "static" / "js" / "chat_markdown.js"
CHAT_JS_PATH = REPO_ROOT / "app" / "static" / "js" / "chat.js"
TEMPLATE_PATH = REPO_ROOT / "app" / "templates" / "chat" / "index.html"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(
    NODE is None, reason="Node.js is required to evaluate the client-side renderer"
)

_NODE_SCRIPT = """
const fs = require("fs");
const md = require(process.env.MARKDOWN_MODULE);
const payloads = JSON.parse(fs.readFileSync(process.env.MARKDOWN_PAYLOADS_FILE, "utf8"));
const results = payloads.map(function (payload) {
  return {
    input: payload,
    rendered: md.renderMarkdown(payload),
    sanitized: md.sanitizeHtml(payload),
  };
});
process.stdout.write(JSON.stringify(results));
"""


@contextlib.contextmanager
def _payload_file(payloads):
    descriptor, name = tempfile.mkstemp(suffix=".json")
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            json.dump(payloads, handle)
        yield Path(name)
    finally:
        with contextlib.suppress(OSError):
            Path(name).unlink()


def _evaluate(payloads):
    with _payload_file(payloads) as payload_path:
        env = dict(os.environ)
        env["MARKDOWN_MODULE"] = str(MODULE_PATH)
        env["MARKDOWN_PAYLOADS_FILE"] = str(payload_path)
        completed = subprocess.run(
            [NODE, "-e", _NODE_SCRIPT],
            capture_output=True,
            text=True,
            env=env,
            check=True,
            timeout=60,
        )
    return json.loads(completed.stdout)


class TestRenderMarkdown:
    def test_script_tag_is_escaped_not_executed(self):
        (result,) = _evaluate(["<script>alert(1)</script>"])
        assert "<script" not in result["rendered"]
        assert "&lt;script&gt;" in result["rendered"]

    def test_event_handler_in_code_span_is_escaped(self):
        (result,) = _evaluate(["`<img src=x onerror=alert(1)>`"])
        assert "<img" not in result["rendered"]
        assert 'onerror="' not in result["rendered"]

    def test_javascript_url_is_not_linked(self):
        (result,) = _evaluate(["[click](javascript:alert(1))"])
        assert "javascript:" not in result["rendered"]
        assert "<a " not in result["rendered"]
        assert "click" in result["rendered"]

    def test_attribute_injection_in_link_is_neutralized(self):
        (result,) = _evaluate(['[x](https://example.com"onmouseover="alert(1))'])
        assert 'onmouseover="' not in result["rendered"]
        assert "<a " in result["rendered"]

    def test_safe_link_opens_with_target_and_noopener(self):
        (result,) = _evaluate(["[docs](https://example.com)"])
        assert 'href="https://example.com"' in result["rendered"]
        assert 'target="_blank"' in result["rendered"]
        assert "noopener" in result["rendered"]

    def test_basic_markdown_still_renders(self):
        (result,) = _evaluate(["**bold** and *italic*"])
        assert "<strong>bold</strong>" in result["rendered"]
        assert "<em>italic</em>" in result["rendered"]


class TestSanitizeHtml:
    def test_removes_script_with_contents(self):
        (result,) = _evaluate(["<p>hello</p><script>alert(1)</script>"])
        assert "<script" not in result["sanitized"]
        assert "alert(1)" not in result["sanitized"]
        assert "hello" in result["sanitized"]

    def test_strips_event_handler_attributes(self):
        (result,) = _evaluate(['<img src="x" onerror="alert(1)">'])
        assert "onerror" not in result["sanitized"]
        assert "<img" not in result["sanitized"]

    def test_blocks_javascript_urls(self):
        (result,) = _evaluate(['<a href="javascript:alert(1)">x</a>'])
        assert "javascript:" not in result["sanitized"]
        assert "href" not in result["sanitized"]

    def test_blocks_entity_obfuscated_javascript_urls(self):
        (result,) = _evaluate(['<a href="jav&#x61;script:alert(1)">x</a>'])
        assert "javascript:" not in result["sanitized"]
        assert "href" not in result["sanitized"]

    def test_rewrites_links_to_safe_target(self):
        (result,) = _evaluate(['<a href="https://example.com">x</a>'])
        assert 'target="_blank"' in result["sanitized"]
        assert "noopener" in result["sanitized"]
        assert "noreferrer" in result["sanitized"]

    def test_unwraps_unknown_tags_and_drops_handlers(self):
        (result,) = _evaluate(['<div onclick="x()">hello</div>'])
        assert "onclick" not in result["sanitized"]
        assert "hello" in result["sanitized"]
        assert "<div" not in result["sanitized"]


class TestWiring:
    def test_chat_template_loads_sanitizer_before_chat(self):
        html = TEMPLATE_PATH.read_text(encoding="utf-8")
        assert "js/chat_markdown.js" in html
        assert html.index("js/chat_markdown.js") < html.index("js/chat.js")

    def test_chat_js_delegates_to_sanitized_renderer(self):
        source = CHAT_JS_PATH.read_text(encoding="utf-8")
        assert "AICAMarkdown" in source
        assert "renderMarkdown" in source
