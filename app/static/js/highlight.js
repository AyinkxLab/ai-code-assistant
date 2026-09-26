// Applies highlight.js to rendered code blocks (issues #44, #94).
//
// The vendored highlight.js build is curated to the languages this app maps
// server-side (see app/services/highlighting.py), so a block's
// `language-<id>` class always matches a registered grammar. For the few
// stored language ids the build cannot ship a grammar for, a tiny
// dependency-free tokenizer keeps the block highlighted instead of letting
// highlight.js auto-detect (and mis-highlight) the content.
//
// Oversized files are left as plain text so a large document never blocks
// rendering, and already-highlighted blocks are skipped so repeated calls
// (e.g. during SSE streaming) never re-render the whole document.
(function () {
  "use strict";

  //: Blocks longer than this are left unhighlighted. The server already marks
  //: very large files unsearchable; this guards the client-only path too.
  var MAX_HIGHLIGHT_CHARS = 200000;

  //: Minimal keyword sets for the stored languages the vendored grammar does
  //: not ship. The tokenizer still handles comments, strings, and numbers.
  var FALLBACK_KEYWORDS = {
    solidity: [
      "pragma", "solidity", "contract", "interface", "library", "function", "modifier",
      "event", "struct", "enum", "mapping", "address", "bool", "string", "bytes", "uint",
      "uint256", "int", "int256", "payable", "public", "private", "internal", "external",
      "view", "pure", "constant", "immutable", "memory", "storage", "calldata", "returns",
      "return", "require", "assert", "revert", "emit", "new", "delete", "if", "else",
      "for", "while", "do", "break", "continue", "try", "catch", "import", "is", "using",
      "constructor", "receive", "fallback", "virtual", "override", "true", "false",
    ],
    hcl: [
      "resource", "data", "variable", "output", "module", "provider", "terraform",
      "locals", "true", "false", "null", "for", "in", "if", "else",
    ],
    graphql: [
      "query", "mutation", "subscription", "fragment", "on", "type", "interface", "input",
      "enum", "scalar", "union", "schema", "directive", "extend", "implements", "true",
      "false", "null",
    ],
  };

  function escapeHtml(text) {
    return String(text == null ? "" : text)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;");
  }

  function escapeRegExp(text) {
    return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  var LANGUAGE_CLASS_RE = /(?:^|\s)(?:language|lang)-([\w+#-]+)/i;

  function languageOf(block) {
    var match = LANGUAGE_CLASS_RE.exec(block.className || "");
    return match ? match[1].toLowerCase() : "";
  }

  // Escapes all non-token text and wraps comments/strings/keywords/numbers.
  function fallbackHighlight(code, language) {
    var keywords = FALLBACK_KEYWORDS[language] || [];
    var kw = keywords.length ? keywords.map(escapeRegExp).join("|") : "$^";
    var pattern = new RegExp(
      "\\/\\/[^\\n]*|#[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/" +
        "|\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*'|`(?:\\\\.|[^`\\\\])*`" +
        "|\\b(?:" + kw + ")\\b" +
        "|\\b\\d+(?:\\.\\d+)?\\b",
      "g",
    );

    var out = "";
    var last = 0;
    var match;
    while ((match = pattern.exec(code)) !== null) {
      out += escapeHtml(code.slice(last, match.index));
      var token = match[0];
      var cls = "tok-keyword";
      if (/^(\/\/|#|\/\*)/.test(token)) cls = "tok-comment";
      else if (/^["'`]/.test(token)) cls = "tok-string";
      else if (/^\d/.test(token)) cls = "tok-number";
      out += '<span class="' + cls + '">' + escapeHtml(token) + "</span>";
      last = match.index + token.length;
      if (token.length === 0) pattern.lastIndex += 1; // guard against zero-width
    }
    out += escapeHtml(code.slice(last));
    return out;
  }

  function apply(root) {
    if (!root || typeof window === "undefined") return 0;
    var blocks = root.querySelectorAll ? root.querySelectorAll("pre code") : [];
    var highlighted = 0;

    Array.prototype.forEach.call(blocks, function (block) {
      // Idempotent: never re-render a block that was already highlighted.
      if (!block || (block.dataset && block.dataset.highlighted === "yes")) return;
      var code = block.textContent || "";
      if (code.length > MAX_HIGHLIGHT_CHARS) return;

      var language = languageOf(block);
      var hasGrammar = !!(
        window.hljs &&
        window.hljs.getLanguage &&
        language &&
        window.hljs.getLanguage(language)
      );

      // No language tag: keep highlight.js auto-detection. Known language:
      // let the vendored grammar do the work.
      if (window.hljs && (!language || hasGrammar)) {
        try {
          window.hljs.highlightElement(block);
          if (block.dataset) block.dataset.highlighted = "yes";
          highlighted += 1;
          return;
        } catch (error) {
          /* fall through to the dependency-free tokenizer */
        }
      }

      // A stored language the vendored build cannot parse: tokenize locally.
      if (language) {
        block.innerHTML = fallbackHighlight(code, language);
        if (block.dataset) block.dataset.highlighted = "yes";
        highlighted += 1;
      }
    });

    return highlighted;
  }

  window.AICASyntaxHighlight = {
    apply: apply,
    MAX_HIGHLIGHT_CHARS: MAX_HIGHLIGHT_CHARS,
    FALLBACK_KEYWORDS: FALLBACK_KEYWORDS,
  };
})();
