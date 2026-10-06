import { afterEach, beforeAll, vi } from "vitest";

// Minimal DOM environment for tests that need to render and query the chat UI.
// Vitest with the jsdom environment already provides these, but this file
// keeps the suite runnable in a bare node environment and centralizes common
// cleanup so individual test files stay small.

const globals = globalThis;

function installMatchMedia() {
  if (typeof globals.matchMedia === "function") {
    return;
  }
  globals.matchMedia = (query) => ({
    matches: false,
    media: query,
    onChange: null,
    addListener: () => {},
    removeListener: () => {},
    addEventListener: () => {},
    removeEventListener: () => {},
    dispatchEvent: () => false,
  });
}

function installResizeObserver() {
  if (typeof globals.ResizeObserver === "function") {
    return;
  }
  globals.ResizeObserver = class {
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

function installIntersectionObserver() {
  if (typeof globals.IntersectionObserver === "function") {
    return;
  }
  globals.IntersectionObserver = class {
    constructor(callback) {
      this.callback = callback;
    }
    observe() {}
    unobserve() {}
    disconnect() {}
  };
}

beforeAll(() => {
  installMatchMedia();
  installResizeObserver();
  installIntersectionObserver();
});

afterEach(() => {
  // Keep tests isolated: clear the body and any pending timers/listeners.
  if (typeof document !== "undefined") {
    document.body.innerHTML = "";
  }
  vi.clearAllTimers();
});
