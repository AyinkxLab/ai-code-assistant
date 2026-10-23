/**
 * Workspace usage dashboard states (#456).
 *
 * The panel must expose explicit loading, empty, populated and error states.
 * An error must replace the loading placeholders, explain what happened, and
 * offer a retry instead of leaving fabricated or stale figures on screen.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { afterEach, beforeAll, describe, expect, it, vi } from "vitest";

const MODULE_PATH = resolve(process.cwd(), "app/static/js/workspace_usage.js");

let usage;

beforeAll(() => {
  window.eval(readFileSync(MODULE_PATH, "utf8"));
  usage = window.AICAWorkspaceUsage;
  if (!usage) {
    throw new Error("workspace_usage.js did not expose window.AICAWorkspaceUsage");
  }
});

afterEach(() => {
  vi.unstubAllGlobals();
  vi.restoreAllMocks();
  document.body.innerHTML = "";
});

function mountDashboard() {
  document.body.innerHTML = `
    <div id="workspace-usage-panel" aria-busy="false">
      <p id="workspace-usage-note"></p>
      <div id="workspace-usage-totals"></div>
      <table><tbody id="workspace-usage-projects"></tbody></table>
      <table><tbody id="workspace-usage-sources"></tbody></table>
      <table><tbody id="workspace-usage-members"></tbody></table>
    </div>`;
  return {
    panel: document.getElementById("workspace-usage-panel"),
    totals: document.getElementById("workspace-usage-totals"),
    projects: document.getElementById("workspace-usage-projects"),
    sources: document.getElementById("workspace-usage-sources"),
    members: document.getElementById("workspace-usage-members"),
    note: document.getElementById("workspace-usage-note"),
  };
}

const EMPTY_DATA = {
  totals: { count: 0, total_tokens: 0, cost_estimate: 0 },
  by_project: [],
  by_source: [],
  by_member: [],
  member_detail: "all",
};

const POPULATED_DATA = {
  currency: { code: "USD", symbol: "$", decimal_places: 4 },
  totals: { count: 2, total_tokens: 1234, cost_estimate: 0.5 },
  by_project: [{ name: "Alpha", totals: { total_tokens: 1234, cost_estimate: 0.5 } }],
  by_source: [{ source: "chat", totals: { total_tokens: 1234, cost_estimate: 0.5 } }],
  by_member: [
    { username: "alice", role: "owner", totals: { total_tokens: 1234, cost_estimate: 0.5 } },
  ],
  member_detail: "all",
};

describe("workspace usage states", () => {
  it("shows an explicit loading state while the request is pending", async () => {
    const view = mountDashboard();
    let resolveFetch;
    const pending = new Promise((resolve) => {
      resolveFetch = resolve;
    });

    const request = usage.load({
      document,
      fetchUsage: () => pending,
    });

    expect(view.panel.getAttribute("aria-busy")).toBe("true");
    const loading = view.totals.querySelector(".usage-state-loading");
    expect(loading).not.toBeNull();
    expect(loading.getAttribute("role")).toBe("status");
    expect(loading.getAttribute("aria-live")).toBe("polite");
    expect(view.totals.textContent).toContain("Loading usage");
    expect(view.projects.textContent).toContain("Loading");

    resolveFetch(EMPTY_DATA);
    await request;

    expect(view.panel.getAttribute("aria-busy")).toBe("false");
  });

  it("renders an explicit empty state without fabricated numbers", async () => {
    const view = mountDashboard();

    await usage.load({
      document,
      fetchUsage: () => Promise.resolve(EMPTY_DATA),
    });

    const empty = view.totals.querySelector(".usage-state-empty");
    expect(empty).not.toBeNull();
    expect(empty.getAttribute("role")).toBe("status");
    expect(view.totals.textContent).toContain("No usage yet");
    expect(view.totals.textContent).not.toMatch(/\b0\b/);
    expect(view.projects.textContent).toContain("No project usage yet.");
    expect(view.sources.textContent).toContain("No usage recorded yet.");
    expect(view.members.textContent).toContain("No member usage yet.");
  });

  it("renders populated totals and breakdown rows", async () => {
    const view = mountDashboard();

    await usage.load({
      document,
      fetchUsage: () => Promise.resolve(POPULATED_DATA),
    });

    expect(view.totals.textContent).toContain("1,234");
    expect(view.totals.textContent).toContain("2");
    expect(view.projects.textContent).toContain("Alpha");
    expect(view.sources.textContent).toContain("chat");
    expect(view.members.textContent).toContain("alice");
    expect(view.note.textContent).toContain("Costs are estimates");
  });

  it("replaces loading placeholders with an error and a working retry", async () => {
    const view = mountDashboard();
    let calls = 0;
    let failing = true;

    const fetchUsage = () => {
      calls += 1;
      if (failing) return Promise.reject(new Error("backend exploded"));
      return Promise.resolve(POPULATED_DATA);
    };

    await usage.load({ document, fetchUsage });

    const error = view.totals.querySelector(".usage-state-error");
    expect(error).not.toBeNull();
    expect(error.getAttribute("role")).toBe("alert");
    expect(view.totals.textContent).toContain("Could not load usage");
    expect(view.totals.textContent).toContain("Please try again");
    expect(view.totals.textContent).not.toContain("backend exploded");
    expect(view.projects.textContent).toContain("Usage unavailable.");
    expect(view.note.textContent).toContain("Use Retry");
    expect(view.panel.getAttribute("aria-busy")).toBe("false");

    const retry = view.totals.querySelector(".usage-retry");
    expect(retry).not.toBeNull();
    expect(retry.textContent).toContain("Retry");

    failing = false;
    retry.click();

    await vi.waitFor(() => {
      expect(view.totals.textContent).toContain("1,234");
    });
    expect(calls).toBe(2);
  });
});

describe("state helpers", () => {
  it("escapes untrusted labels", () => {
    const html = usage.stateHtml("empty", {
      title: "<img src=x onerror=alert(1)>",
      message: "<script>alert(1)</script>",
    });
    const container = document.createElement("div");
    container.innerHTML = html;

    expect(container.querySelector("img")).toBeNull();
    expect(container.querySelector("script")).toBeNull();
    expect(container.textContent).toContain("<img src=x onerror=alert(1)>");
  });

  it("binds the retry handler", () => {
    const container = document.createElement("div");
    container.innerHTML = usage.errorStateHtml();
    const handler = vi.fn();

    usage.bindRetry(container, handler);
    container.querySelector(".usage-retry").click();

    expect(handler).toHaveBeenCalledTimes(1);
  });

  it("can omit the retry affordance", () => {
    const container = document.createElement("div");
    container.innerHTML = usage.errorStateHtml({ retry: false });

    expect(container.querySelector(".usage-retry")).toBeNull();
  });
});
