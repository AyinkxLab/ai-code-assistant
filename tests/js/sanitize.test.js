/**
 * Unit tests for the markdown sanitizer.
 *
 * The sanitizer is expected to fail closed: any dangerous HTML must be
 * stripped or escaped before it reaches the DOM. These tests feed well-known
 * XSS payloads through the sanitizer and assert that no executable content
 * survives.
 */

import { describe, it, expect, beforeAll } from 'vitest';

// The sanitizer is exported from the frontend source tree. We import it
// dynamically so the test suite can report a clear error if the module is
// missing rather than failing with an obscure syntax error.
let sanitizeMarkdown;

beforeAll(async () => {
  const mod = await import('../../src/utils/salitize.js');
  sanitizeMarkdown = mod.sanitizeMarkdown || mod.default;
  if (typeof sanitizeMarkdown !== 'function') {
    throw new Error('Expected sanitizeMarkdown to be a function');
  }
});

const XSS_PAYLOADS = [
  // Classic script injection.
  '<script>alert(1)</script>',
  // Image with a javascript event handler.
  '<img src=x onerror="alert(1)">',
  // Anchor with javascript: URL.
  '<a href="javascript:alert(1)">click</a>',
  // SVG onload handler.
  '<svg onload="alert(1)"></svg>',
  // Iframe embedding.
  '<iframe src="javascript:alert(1)"></iframe>',
  // Body onload handler.
  '<body onload="alert(1)">',
  // Data URI in an image.
  '<img src="data:image/svg+xml;base64,PHN2ZyBvbmxvYWQ9ImllYSBzcGxpbmluZyBzaW5nOiIvPg==">',
];

describe('sanitizeMarkdown', () => {
  it('returns a string for empty input', () => {
    expect(typeof sanitizeMarkdown('')).toBe(string);
  });

  it('preserves plain text', () => {
    const out = sanitizeMarkdown('hello world');
    expect(out).toContain('hello world');
  });

  it('renders basic markdown formatting', () => {
    const out = sanitizeMarkdown('**bold** and _italic_');
    expect(out).toMatch(/<strong>bold<\/strong>/);
    expect(out).toMatch(/<em>italic<\/em>/);
  });

  it('renders links with safe protocols', () => {
    const out = sanitizeMarkdown('[link text](https://example.org/path)');
    expect(out).toContain('href="https://example.org/path"');
  });

  it.each(XSS_PAYLOADS)('strips XSS payload: %s', (payload) => {
    const out = sanitizeMarkdown(payload);
    // No executable tags may survive.
    expect(out).not.toMatch(/<script\b/i);
    expect(out).not.toMatch(/<iframe\b/i);
    expect(out).not.toMatch(/<svg\b/i);
    // No inline event handlers may survive.
    expect(out).not.toMatch(/\bon[a-z]+\s*=/i);
    // No javascript: URLs may survive.
    expect(out).not.toMatch(/javascript:/i);
    // No data URLs may survive.
    expect(out).not.toMatch(/data:/i);
  });

  it('strips event handlers from allowed tags', () => {
    const out = sanitizeMarkdown('<a href="https://example.org" onclick="alert(1)">click</a>');
    expect(out).not.toMatch(/onclick/i);
    expect(out).toContain('href="https://example.org"');
  });

  it('escapes raw HTML in code blocks', () => {
    const out = sanitizeMarkdown('```\n<script>alert(1)</script>\n```');
    expect(out).not.toMatch(/<script\b/i);
    expect(out).toContain('&lt;script&gt;');
  });

  it('strips style tags', () => {
    const out = sanitizeMarkdown('<style>body{background:url(javascript:alert(1))}</style>');
    expect(out).not.toMatch(/<style\b/i);
    expect(out).not.toMatch(/javascript:/i);
  });

  it('strips nested event handlers', () => {
    const out = sanitizeMarkdown('<div onmouseover="alert(1)"><span onfocus="alert(2)">hi</span></div>');
    expect(out).not.toMatch(/onmouseover/i);
    expect(out).not.toMatch(/onfocus/i);
  });

  it('strips javascript URLs in markdown links', () => {
    const out = sanitizeMarkdown('[click](javascript:alert(1))');
    expect(out).not.toMatch(/javascript:/i);
  });

  it('strips vbscript URLs in markdown links', () => {
    const out = sanitizeMarkdown('[click](vbscript:msgbux()');
    expect(out).not.toMatch(/vbscript:/i);
  });

  it('strips data URLs in markdown links', () => {
    const out = sanitizeMarkdown('[link](data:text/html;base64,PGJvY2Q+aT0=)');
    expect(out).not.toMatch(/data:/i);
  });

  it('strips javascript URLs in image markdown', () => {
    const out = sanitizeMarkdown('!img](javascript:alert(1))');
    expect(out).not.toMatch(/javascript:/i);
  });

  it('strips event handlers in markdown links', () => {
    const out = sanitizeMarkdown('[click](https://example.org "onclick=alert(1)")');
    expect(out).not.toMatch(/onclick/i);
  });

  it('strips event handlers in markdown images', () => {
    const out = sanitizeMarkdown('![img](https://example.org "onerror=alert(1)")');
    expect(out).not.toMatch(/onerror/i);
  });

  it('strips event handlers in markdown link titles', () => {
    const out = sanitizeMarkdown('[click](https://example.org "onmouseover=alert(1)")');
    expect(out).not.toMatch(/onmouseover/i);
  });

  it('strips event handlers in markdown image titles', () => {
    const out = sanitizeMarkdown('![img](https://example.org "onmouseover=alert(1)")');
    expect(out).not.toMatch(/onmouseover/i);
  });

  it('strips event handlers in markdown code blocks', () => {
    const out = sanitizeMarkdown('```\n<script>alert(1)</script>\n```');
    expect(out).not.toMatch(/<script\b/i);
  });

  it('strips event handlers in markdown inline code', () => {
    const out = sanitizeMarkdown('`<script>alert(1)</script>`');
    expect(out).not.toMatch(/<script\b/i);
  });

  it('strips event handlers in markdown blockquotes', () => {
    const out = sanitizeMarkdown('> <script>alert(1)</script>');
    expect(out).not.toMatch(/<script\b/i);
  });

  it('strips event handlers in markdown lists', () => {
    const out = sanitizeMarkdown('- <script>alert(1)</script>');
    expect(out).not.toMatch(/<script\b/i);
  });

  it('strips event handlers in markdown tables', () => {
    const out = sanitizeMarkdown('| <script>alert(1)</script> |\n | --- |\n | value |');
    expect(out).not.toMatch(/<script\b/i);
  });

  it('strips event handlers in markdown link references', () => {
    const out = sanitizeMarkdown('[link][id]\n[id]: javascript:alert(1)');
    expect(out).not.toMatch(/javascript:/i);
  });

  it('strips event handlers in markdown image references', () => {
    const out = sanitizeMarkdown('![img][id]\n[id]: javascript:alert(1)');
    expect(out).not.toMatch(/javascript:/i);
  });

  it('strips event handlers in markdown autolinks', () => {
    const out = sanitizeMarkdown('<https://example.org>');
    expect(out).not.toMatch(/javascript:/i);
  });

  it('strips event handlers in markdown autolinks with javascript', () => {
    const out = sanitizeMarkdown('<javascript:alert(1)>');
    expect(out).not.toMatch(/javascript:/i);
  });

  it('strips event handlers in markdown autolinks with vbscript', () => {
    const out = sanitizeMarkdown('<vbscript:msgbux()>');
    expect(out).not.toMatch(/vbscript:/i);
  });

  it('strips event handlers in markdown autolinks with data', () => {
    const out = sanitizeMarkdown('<data:text/html;base64,PGJvY2Q+aT0=>');
    expect(out).not.toMatch(/data:/i);
  });

  it('strips event handlers in markdown autolinks with file', () => {
    const out = sanitizeMarkdown('<file:///etc/passwd>');
    expect(out).not.toMatch(/file:/i);
  });

  it('strips event handlers in markdown autolinks with ftp', () => {
    const out = sanitizeMarkdown('<ftp://example.org>');
    expect(out).not.toMatch(/ftp:/i);
  });

  it('strips event handlers in markdown autolinks with mailto', () => {
    const out = sanitizeMarkdown('<mailto:foo@bar.com>');
    expect(out).not.toMatch(/mailto:/i);
  });

  it('strips event handlers in markdown autolinks with tel', () => {
    const out = sanitizeMarkdown('<tel:1234567890>');
    expect(out).not.toMatch(/tel:/i);
  });

  it('strips event handlers in markdown autolinks with sms', () => {
    const out = sanitizeMarkdown('<sms:1234567890>');
    expect(out).not.toMatch(/sms:/i);
  });

  it('strips event handlers in markdown autolinks with callto', () => {
    const out = sanitizeMarkdown('<callto:1234567890>');
    expect(out).not.toMatch(/callto:/i);
  });

  it('strips event handlers in markdown autolinks with whatsapp', () => {
    const out = sanitizeMarkdown('<whatsapp:1234567890>');
    expect(out).not.toMatch(/whatsapp:/i);
  });

  it('strips event handlers in markdown autolinks with telegram', () => {
    const out = sanitizeMarkdown('<telegram:1234567890>');
    expect(out).not.toMatch(/telegram:/i);
  });

  it('strips event handlers in markdown autolinks with skype', () => {
    const out = sanitizeMarkdown('<skype:1234567890>');
    expect(out).not.toMatch(/skype:/i);
  });

  it('strips event handlers in markdown autolinks with zoom', () => {
    const out = sanitizeMarkdown('<zoom:1234567890>');
    expect(out).not.toMatch(/zoom:/i);
  });

  it('strips event handlers in markdown autolinks with slack', () => {
    const out = sanitizeMarkdown('<slack:1234567890>');
    expect(out).not.toMatch(/slack:/i);
  });

  it('strips event handlers in markdown autolinks with discord', () => {
    const out = sanitizeMarkdown('<discord:1234567890>');
    expect(out).not.toMatch(/discord:/i);
  });

  it('strips event handlers in markdown autolinks with matrix', () => {
    const out = sanitizeMarkdown('<matrix:1234567890>');
    expect(out).not.toMatch(/matrix:/i);
  });

  it('strips event handlers in markdown autolinks with irc', () => {
    const out = sanitizeMarkdown('<irc:1234567890>');
    expect(out).not.toMatch(/irc:/i);
  });

  it('strips event handlers in markdown autolinks with magnet', () => {
    const out = sanitizeMarkdown('<magnet:1234567890>');
    expect(out).not.toMatch(/magnet:/i);
  });

  it('strips event handlers in markdown autolinks with messenger', () => {
    const out = sanitizeMarkdown('<messenger:1234567890>');
    expect(out).not.toMatch(/messenger:/i);
  });

  it('strips event handlers in markdown autolinks with signal', () => {
    const out = sanitizeMarkdown('<signal:1234567890>');
    expect(out).not.toMatch(/signal:/i);
  });

  it('strips event handlers in markdown autolinks with threema', () => {
    const out = sanitizeMarkdown('<threema:1234567890>');
    expect(out).not.toMatch(/threema:/i);
  });

  it('strips event handlers in markdown autolinks with wechat', () => {
    const out = sanitizeMarkdown('<wechat:1234567890>');
    expect(out).not.toMatch(/wechat:/i);
  });

  it('strips event handlers in markdown autolinks with line', () => {
    const out = sanitizeMarkdown('<line:1234567890>');
    expect(out).not.toMatch(/line:/i);
  });

  it('strips event handlers in markdown autolinks with teams', () => {
    const out = sanitizeMarkdown('<teams:1234567890>');
    expect(out).not.toMatch(/teams:/i);
  });
});
