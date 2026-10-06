/**
 * Unit tests for the chat markdown sanitizer.
 *
 * `app/static/js/chat_markdown.js` renders untrusted model output, so the
 * sanitizer must fail closed: any dangerous HTML has to be stripped or
 * escaped before it reaches the DOM. These tests feed well-known XSS payloads
 * through the module and assert that no executable content survives.
 *
 * The module is a UMD bundle that attaches itself to `window.AICAMarkdown`,
 * so it is evaluated into the jsdom window rather than imported.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { beforeAll, describe, expect, it } from "vitest";

const MODULE_PATH = resolve(process.cwd(), "app/static/js/chat_markdown.js");

let markdown;

beforeAll(() => {
  window.eval(readFileSync(MODULE_PATH, "utf8"));
  markdown = window.AICAMarkdown;
  if (!markdown || typeof markdown.renderMarkdown !== "function") {
    throw new Error("chat_markdown.js did not expose window.AICAMarkdown.renderMarkdown");
  }
});

const XSS_PAYLOADS = [
  "<script>alert(1)</script>",
  '<img src=x onerror="alert(1)">',
  '<a href="javascript:alert(1)">click</a>',
  '<svg onload="alert(1)"></svg>',
  '<iframe src="javascript:alert(1)"></iframe>',
  '<body onload="alert(1)">',
  '<div onclick="alert(1)">x</div>',
];

describe("escapeHtml", () => {
  it("escapes the characters that can break out of markup", () => {
    expect(markdown.escapeHtml("<b>&\"'")).toBe("&lt;b&gt;&amp;&quot;&#39;");
  });
});

describe("isSafeUrl", () => {
  it.each([
    ["https://example.org/path", true],
    ["/chat/conversations/1", true],
    ["#anchor", true],
    ["javascript:alert(1)", false],
    ["JaVaScRiPt:alert(1)", false],
    ["data:text/html;base64,PHNjcmlwdD4=", false],
    ["vbscript:msgbox(1)", false],
  ])("treats %s as safe=%s", (url, expected) => {
    expect(markdown.isSafeUrl(url)).toBe(expected);
  });
});

describe("sanitizeHtml", () => {
  it("returns a string for empty input", () => {
    expect(typeof markdown.sanitizeHtml("")).toBe("string");
  });

  it.each(XSS_PAYLOADS)("removes executable content from %s", (payload) => {
    const out = markdown.sanitizeHtml(payload);
    expect(out).not.toMatch(/<script/i);
    expect(out).not.toMatch(/on\w+\s*=/i);
    expect(out).not.toMatch(/javascript:/i);
    expect(out).not.toMatch(/<iframe/i);
    expect(out).not.toMatch(/<svg/i);
  });

  it("keeps ordinary markup", () => {
    const out = markdown.sanitizeHtml('<p class="x">hello <strong>there</strong></p>');
    expect(out).toContain("<p");
    expect(out).toContain("<strong>there</strong>");
  });
});

describe("renderMarkdown", () => {
  it("renders basic inline markdown", () => {
    const out = markdown.renderMarkdown("**bold** and *italic*");
    expect(out).toMatch(/<strong>bold<\/strong>/);
    expect(out).toMatch(/<em>italic<\/em>/);
  });

  it("renders inline code", () => {
    const out = markdown.renderMarkdown("`code`");
    expect(out).toContain('<code class="inline-code">code</code>');
  });

  it("renders fenced code blocks", () => {
    const out = markdown.renderMarkdown("```js\nconst a = 1;\n```");
    expect(out).toContain('<pre class="code-block">');
    expect(out).toContain('<code class="language-js">');
    expect(out).toContain("const a = 1;");
  });

  it("renders unordered lists", () => {
    const out = markdown.renderMarkdown("- one\n- two");
    expect(out).toContain("<ul>");
    expect(out).toContain("<li>one</li>");
    expect(out).toContain("<li>two</li>");
  });

  it("renders ordered lists", () => {
    const out = markdown.renderMarkdown("1. one\n2. two");
    expect(out).toContain("<ol>");
    expect(out).toContain("<li>one</li>");
  });

  it("keeps safe link protocols", () => {
    const out = markdown.renderMarkdown("[link](https://example.org/path)");
    expect(out).toContain('href="https://example.org/path"');
  });

  it("does not emit javascript: URLs", () => {
    const out = markdown.renderMarkdown("[click](javascript:alert(1))");
    expect(out).not.toMatch(/javascript:/i);
    expect(out).not.toContain("<a");
  });

  it.each(XSS_PAYLOADS)("escapes rather than executes %s", (payload) => {
    const out = markdown.renderMarkdown(payload);
    // Raw HTML in model output must be escaped, never emitted as markup.
    expect(out).toContain("&lt;");
    expect(out).not.toMatch(/<script/i);
    expect(out).not.toMatch(/<iframe/i);
    expect(out).not.toMatch(/<svg/i);
    expect(out).not.toMatch(/<img/i);
    expect(out).not.toMatch(/<body/i);
    expect(out).not.toMatch(/<div/i);
  });

  it("escapes raw HTML in ordinary text", () => {
    const out = markdown.renderMarkdown("<b>not bold</b>");
    expect(out).not.toMatch(/<b>not bold<\/b>/);
    expect(out).toContain("&lt;b&gt;");
  });
});
