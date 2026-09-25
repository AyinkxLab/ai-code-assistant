// AI Code Assistant — XSS-safe Markdown rendering for chat output.
//
// The chat UI renders untrusted model output, so all text is escaped and the
// generated markup is passed through `sanitizeHtml` before it is returned to
// the caller for insertion into the DOM. The module is dependency-free and
// deliberately DOM-free, so the exact same code runs in the browser and under
// Node in the unit tests (see tests/test_chat_markdown_sanitize.py).
//
// Issue #35: sanitize rendered Markdown in the chat UI against XSS.

(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.AICAMarkdown = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  // Only these tags may survive sanitization. Anything else is unwrapped (its
  // text content is kept) unless it appears in DROP_WITH_CONTENT.
  var ALLOWED_ATTRIBUTES = {
    a: { href: true, target: true, rel: true, class: true },
    code: { class: true },
    pre: { class: true },
    span: { class: true },
    strong: {},
    em: {},
    ul: {},
    ol: {},
    li: {},
    p: {},
    br: {},
    blockquote: {},
    h1: {},
    h2: {},
    h3: {},
    h4: {},
    h5: {},
    h6: {},
  };

  // Elements removed together with their contents. Their payload must never be
  // unwrapped into the page.
  var DROP_WITH_CONTENT = [
    "script",
    "style",
    "iframe",
    "object",
    "embed",
    "svg",
    "math",
    "template",
    "noscript",
    "title",
    "textarea",
    "xmp",
    "plaintext",
  ];

  var SAFE_URL_SCHEMES = { http: true, https: true, mailto: true };

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;")
      .replace(/'/g, "&#39;");
  }

  function decodeEntities(value) {
    return String(value)
      .replace(/&#x([0-9a-f]+);?/gi, function (_match, hex) {
        var code = parseInt(hex, 16);
        return isFinite(code) ? String.fromCharCode(code) : "";
      })
      .replace(/&#(\d+);?/g, function (_match, dec) {
        var code = parseInt(dec, 10);
        return isFinite(code) ? String.fromCharCode(code) : "";
      })
      .replace(/&colon;/gi, ":")
      .replace(/&tab;/gi, "\t")
      .replace(/&newline;/gi, "\n")
      .replace(/&sol;/gi, "/")
      .replace(/&amp;/gi, "&");
  }

  // Reject schemes such as `javascript:`, `data:` and `vbscript:` (including
  // entity-obfuscated and whitespace/control-character variants) while still
  // allowing relative, fragment and protocol-relative URLs.
  function isSafeUrl(raw) {
    if (raw === undefined || raw === null) return false;
    var url = decodeEntities(raw).replace(/[\u0000-\u0020\u007f-\u009f]/g, "");
    var scheme = /^([a-zA-Z][a-zA-Z0-9+.-]*):/.exec(url);
    if (!scheme) return true;
    return Object.prototype.hasOwnProperty.call(SAFE_URL_SCHEMES, scheme[1].toLowerCase());
  }

  function filterAttributes(tag, rawAttributes, allowed) {
    var parts = [];
    var seen = {};
    var attrRe = /([^\s"'<>/=]+)(?:\s*=\s*(?:"([^"]*)"|'([^']*)'|([^\s"'`=<>]+)))?/g;
    var match;
    while ((match = attrRe.exec(rawAttributes)) !== null) {
      var name = match[1].toLowerCase();
      if (name.slice(0, 2) === "on") continue; // never allow event handlers
      if (!Object.prototype.hasOwnProperty.call(allowed, name) || seen[name]) continue;
      var value =
        match[2] !== undefined
          ? match[2]
          : match[3] !== undefined
            ? match[3]
            : match[4] !== undefined
              ? match[4]
              : "";
      if (name === "href" && !isSafeUrl(value)) continue;
      if (name === "class") value = value.replace(/[^a-zA-Z0-9 _-]/g, "");
      seen[name] = true;
      parts.push(name + '="' + escapeHtml(value) + '"');
    }
    if (tag === "a") {
      if (!seen.target) parts.push('target="_blank"');
      if (!seen.rel) parts.push('rel="noopener noreferrer"');
    }
    return parts.join(" ");
  }

  // Allowlist sanitizer: removes comments and dangerous elements, unwraps
  // unknown tags, drops event-handler/style attributes, and validates URLs.
  function sanitizeHtml(html) {
    var output = String(html).replace(/<!--[\s\S]*?-->/g, "");

    DROP_WITH_CONTENT.forEach(function (tag) {
      var paired = new RegExp("<" + tag + "\\b[^>]*>[\\s\\S]*?<\\/" + tag + "\\s*>", "gi");
      var lone = new RegExp("<\\/?" + tag + "\\b[^>]*>", "gi");
      output = output.replace(paired, "").replace(lone, "");
    });

    var tagRe = /<(\/?)([a-zA-Z][a-zA-Z0-9]*)((?:[^>"']|"[^"]*"|'[^']*')*)>/g;
    return output.replace(tagRe, function (_match, closing, name, attrs) {
      var tag = name.toLowerCase();
      if (!Object.prototype.hasOwnProperty.call(ALLOWED_ATTRIBUTES, tag)) {
        return ""; // unwrap unknown tags, keeping their text content
      }
      if (closing) return "</" + tag + ">";
      var filtered = filterAttributes(tag, attrs, ALLOWED_ATTRIBUTES[tag]);
      return "<" + tag + (filtered ? " " + filtered : "") + ">";
    });
  }

  function renderInline(text) {
    return escapeHtml(text)
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>')
      .replace(/\[([^\]]+)\]\(([^)\s]+)\)/g, function (_match, label, url) {
        if (!isSafeUrl(url)) return label; // render unsafe links as plain text
        return (
          '<a href="' +
          escapeHtml(url) +
          '" target="_blank" rel="noopener noreferrer">' +
          label +
          "</a>"
        );
      });
  }

  // Minimal markdown renderer: code blocks, inline code, headings, bold,
  // italics, links, and unordered lists. Output is always sanitized.
  function renderMarkdown(text) {
    var lines = String(text).split("\n");
    var html = "";
    var inCode = false;
    var codeLang = "";
    var codeLines = [];
    var listOpen = false;

    function flushList() {
      if (listOpen) {
        html += "</ul>\n";
        listOpen = false;
      }
    }

    lines.forEach(function (line) {
      var codeMatch = line.match(/^```(\w*)/);
      if (codeMatch) {
        flushList();
        if (inCode) {
          html +=
            '<pre class="code-block"><code class="language-' +
            escapeHtml(codeLang) +
            '">' +
            escapeHtml(codeLines.join("\n")) +
            "</code></pre>\n";
          inCode = false;
          codeLines = [];
        } else {
          inCode = true;
          codeLang = codeMatch[1] || "";
        }
        return;
      }
      if (inCode) {
        codeLines.push(line);
        return;
      }

      if (/^\s*-\s+/.test(line) || /^\s*\*\s+/.test(line)) {
        if (!listOpen) {
          html += "<ul>\n";
          listOpen = true;
        }
        html += "<li>" + renderInline(line.replace(/^\s*[-*]\s+/, "")) + "</li>\n";
        return;
      }
      flushList();

      if (/^#{1,4}\s/.test(line)) {
        var level = line.match(/^(#{1,4})\s/)[1].length;
        html +=
          "<h" + level + ">" + renderInline(line.replace(/^#{1,4}\s/, "")) + "</h" + level + ">\n";
      } else if (/^\s*$/.test(line)) {
        html += "<br>\n";
      } else {
        html += "<p>" + renderInline(line) + "</p>\n";
      }
    });

    flushList();
    if (inCode) {
      html +=
        '<pre class="code-block"><code class="language-' +
        escapeHtml(codeLang) +
        '">' +
        escapeHtml(codeLines.join("\n")) +
        "</code></pre>\n";
    }
    return sanitizeHtml(html);
  }

  return {
    escapeHtml: escapeHtml,
    isSafeUrl: isSafeUrl,
    sanitizeHtml: sanitizeHtml,
    renderMarkdown: renderMarkdown,
  };
});
