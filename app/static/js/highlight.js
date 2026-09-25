// Applies highlight.js to rendered code blocks (issue #44).
//
// Safe no-op when the library failed to load, and graceful for unknown
// languages: hljs.highlightElement leaves a block it cannot parse untouched and
// we swallow any error, so rendering never breaks the SSE streaming flow.
(function () {
  "use strict";

  function apply(root) {
    if (!root || typeof window === "undefined" || !window.hljs) return 0;
    var blocks = root.querySelectorAll ? root.querySelectorAll("pre code") : [];
    var highlighted = 0;
    Array.prototype.forEach.call(blocks, function (block) {
      try {
        window.hljs.highlightElement(block);
        highlighted += 1;
      } catch (error) {
        /* unknown language or highlight failure: keep the plain text */
      }
    });
    return highlighted;
  }

  window.AICASyntaxHighlight = { apply: apply };
})();
