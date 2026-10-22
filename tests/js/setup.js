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

// jsdom does not implement the canvas 2D backend: `getContext("2d")` logs a
// "Not implemented" error and returns null, so any chart that draws on a
// canvas crashes during construction. Replace it with a no-op recording
// context so the chart code (layout, scaling, data handling) actually runs.
const CANVAS_CONTEXT_METHODS = [
  "arc",
  "beginPath",
  "clearRect",
  "closePath",
  "fill",
  "fillRect",
  "fillText",
  "lineTo",
  "moveTo",
  "restore",
  "rotate",
  "save",
  "scale",
  "setLineDash",
  "stroke",
  "strokeRect",
  "translate",
];

function createCanvasContext2d(canvas) {
  const context = {
    canvas,
    fillStyle: "#000000",
    strokeStyle: "#000000",
    lineWidth: 1,
    lineCap: "butt",
    lineJoin: "miter",
    font: "10px sans-serif",
    textAlign: "start",
    textBaseline: "alphabetic",
    measureText: (text) => ({ width: String(text).length * 6 }),
  };
  for (const method of CANVAS_CONTEXT_METHODS) {
    context[method] = () => {};
  }
  return context;
}

function installCanvasContext() {
  if (typeof globals.HTMLCanvasElement !== "function") {
    return;
  }
  const proto = globals.HTMLCanvasElement.prototype;
  if (proto.__testCanvasContextInstalled) {
    return;
  }
  const originalGetContext = proto.getContext;

  proto.getContext = function getContext(type, ...args) {
    if (type !== "2d") {
      return originalGetContext ? originalGetContext.call(this, type, ...args) : null;
    }
    if (!this.__testCanvasContext2d) {
      this.__testCanvasContext2d = createCanvasContext2d(this);
    }
    return this.__testCanvasContext2d;
  };
  proto.__testCanvasContextInstalled = true;
}

beforeAll(() => {
  installMatchMedia();
  installResizeObserver();
  installIntersectionObserver();
  installCanvasContext();
});

afterEach(() => {
  // Keep tests isolated: clear the body and any pending timers/listeners.
  if (typeof document !== "undefined") {
    document.body.innerHTML = "";
  }
  vi.clearAllTimers();
});
