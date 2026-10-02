"""Color-coded inline diff highlighting (issue #71).

`app/static/js/github.js` exposes `renderPatch`, which classifies each unified
diff line as added (`+`), removed (`-`), hunk (`@@`), file metadata, or context,
and HTML-escapes every line. This test evaluates the checked-in script under
Node (no npm packages) with a tiny DOM shim for `escapeHtml`.
"""

import json
import os
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
GITHUB_JS = REPO_ROOT / "app" / "static" / "js" / "github.js"
PULL_JS = REPO_ROOT / "app" / "static" / "js" / "github_pull.js"
CSS = REPO_ROOT / "app" / "static" / "css" / "style.css"

NODE = shutil.which("node")

pytestmark = pytest.mark.skipif(NODE is None, reason="Node.js is required to evaluate the renderer")

_NODE_SCRIPT = """
global.window = {};
global.document = {
  createElement: function () {
    var el = { _t: "" };
    Object.defineProperty(el, "textContent", {
      set: function (v) { el._t = String(v == null ? "" : v); },
      get: function () { return el._t; },
    });
    Object.defineProperty(el, "innerHTML", {
      get: function () {
        return el._t
          .replace(/&/g, "&amp;")
          .replace(/</g, "&lt;")
          .replace(/>/g, "&gt;")
          .replace(/"/g, "&quot;");
      },
    });
    return el;
  },
  querySelector: function () { return null; },
};
require(process.env.GITHUB_MODULE);
var GH = global.window.GitHub;
var patch = [
  "@@ -1,2 +1,2 @@",
  "-old line",
  "+new <script>alert(1)</script>",
  " context line",
].join("\\n");
process.stdout.write(JSON.stringify({
  html: GH.renderPatch(patch),
  empty: GH.renderPatch(null),
}));
"""


def _render():
    env = dict(os.environ)
    env["GITHUB_MODULE"] = str(GITHUB_JS)
    completed = subprocess.run(
        [NODE, "-e", _NODE_SCRIPT],
        capture_output=True,
        text=True,
        env=env,
        check=True,
        timeout=60,
    )
    return json.loads(completed.stdout)


class TestRenderPatch:
    def test_added_removed_and_context_lines_are_classified(self):
        result = _render()
        html = result["html"]
        assert 'class="diff-line diff-added">+new ' in html
        assert 'class="diff-line diff-removed">-old line' in html
        # A context line is neutral: only the base class.
        assert 'class="diff-line"> context line' in html
        assert "diff-hunk" in html

    def test_active_content_in_a_diff_line_is_escaped(self):
        html = _render()["html"]
        assert "<script>" not in html
        assert "&lt;script&gt;" in html

    def test_missing_patch_is_reported(self):
        empty = _render()["empty"]
        assert "no inline diff available" in empty


class TestWiring:
    def test_pull_view_uses_the_color_coded_renderer(self):
        source = PULL_JS.read_text(encoding="utf-8")
        assert "GH.renderPatch(" in source
        # The old monochrome <pre> wrapper is gone.
        assert "<pre" not in source

    def test_css_defines_added_and_removed_colors(self):
        css = CSS.read_text(encoding="utf-8")
        assert ".diff-added" in css
        assert ".diff-removed" in css
