/**
 * Unit tests for the pure helpers the chat UI exposes on `window.AICA`.
 *
 * The chat client is a single IIFE that talks to the DOM, so `chat.js`
 * publishes a small testing surface. These tests cover the helpers that the
 * streaming UI depends on: HTML escaping, markdown dispatch, relative
 * timestamps, CSRF lookup, and the "is the user pinned to the bottom?"
 * decision that stops a streaming token update from yanking the scroll
 * position (issue #9).
 */
import { beforeAll, beforeEach, describe, expect, it } from "vitest";

beforeAll(async () => {
  document.head.innerHTML = '<meta name="csrf-token" content="test-token">';
  document.body.innerHTML = '<div id="chat-messages"></div>';
  await import("../../app/static/js/chat.js");
});

beforeEach(() => {
  document.body.innerHTML = '<div id="chat-messages"></div>';
});

function aica() {
  return window.AICA;
}

describe("chat client testing surface", () => {
  it("publishes its helpers", () => {
    const api = aica();
    expect(api).toBeTruthy();
    for (const name of ["escapeHtml", "formatRelativeTime", "renderMarkdown", "isNearBottom", "getCsrf"]) {
      expect(typeof api[name]).toBe("function");
    }
  });

  it("escapes HTML so model output cannot inject markup", () => {
    expect(aica().escapeHtml('<img src=x onerror="alert(1)">')).not.toContain("<img");
    expect(aica().escapeHtml("<b>")).toBe("&lt;b&gt;");
  });

  it("reads the CSRF token from the meta tag", () => {
    expect(aica().getCsrf()).toBe("test-token");
  });

  it("returns an empty string for missing or invalid timestamps", () => {
    expect(aica().formatRelativeTime("")).toBe("");
    expect(aica().formatRelativeTime("not-a-date")).toBe("");
  });

  it("formats a past timestamp in relative terms", () => {
    const anHourAgo = new Date(Date.now() - 3600 * 1000).toISOString();
    expect(aica().formatRelativeTime(anHourAgo)).toMatch(/hour/i);
  });

  it("reports being pinned to the bottom of the transcript", () => {
    expect(aica().isNearBottom()).toBe(true);
  });

  it("delegates markdown rendering to the sanitizing renderer", () => {
    window.eval(
      'window.AICAMarkdown = { renderMarkdown: function (text) { return "<em>" + text + "</em>"; } };',
    );
    expect(aica().renderMarkdown("hi")).toBe("<em>hi</em>");
  });
});
