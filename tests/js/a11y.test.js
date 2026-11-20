/**
 * Keyboard behaviour for modal dialogs (#713).
 *
 * The feature scripts only toggle the `hidden` attribute. These tests pin the
 * shared contract in app/static/js/a11y.js: focus moves into the dialog, Tab
 * wraps at both ends, Escape closes it, and focus returns to the trigger.
 */
import { readFileSync } from "node:fs";
import { resolve } from "node:path";

import { beforeAll, beforeEach, describe, expect, it } from "vitest";

const MODULE_PATH = resolve(process.cwd(), "app/static/js/a11y.js");

let a11y;

function mountDialog() {
  document.body.innerHTML = `
    <button id="opener" type="button">New workspace</button>
    <div id="dialog" class="modal" role="dialog" aria-modal="true" hidden>
      <button id="close" type="button">Close</button>
      <input id="name" type="text" aria-label="Name">
      <button id="save" type="button">Save</button>
    </div>`;
  return {
    opener: document.getElementById("opener"),
    dialog: document.getElementById("dialog"),
    close: document.getElementById("close"),
    name: document.getElementById("name"),
    save: document.getElementById("save"),
  };
}

function openWithKeyboard(ui) {
  a11y.init();
  ui.opener.focus();
  ui.opener.click();
  ui.dialog.hidden = false;
  return new Promise((resolve) => setTimeout(resolve, 0));
}

beforeAll(() => {
  window.eval(readFileSync(MODULE_PATH, "utf8"));
  a11y = window.AICA11y;
  if (!a11y) {
    throw new Error("a11y.js did not expose window.AICA11y");
  }
});

beforeEach(() => {
  document.body.innerHTML = "";
});

describe("modal focus management", () => {
  it("focuses the first control when the dialog opens", async () => {
    const ui = mountDialog();
    await openWithKeyboard(ui);

    expect(ui.dialog.contains(document.activeElement)).toBe(true);
    expect(document.activeElement).toBe(ui.close);
  });

  it("wraps Tab from the last control to the first", async () => {
    const ui = mountDialog();
    await openWithKeyboard(ui);

    ui.save.focus();
    ui.save.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Tab", bubbles: true, cancelable: true })
    );

    expect(document.activeElement).toBe(ui.close);
  });

  it("wraps Shift+Tab from the first control to the last", async () => {
    const ui = mountDialog();
    await openWithKeyboard(ui);

    ui.close.focus();
    ui.close.dispatchEvent(
      new KeyboardEvent("keydown", {
        key: "Tab",
        shiftKey: true,
        bubbles: true,
        cancelable: true,
      })
    );

    expect(document.activeElement).toBe(ui.save);
  });

  it("closes on Escape and returns focus to the trigger", async () => {
    const ui = mountDialog();
    await openWithKeyboard(ui);

    ui.dialog.dispatchEvent(
      new KeyboardEvent("keydown", { key: "Escape", bubbles: true, cancelable: true })
    );

    expect(ui.dialog.hidden).toBe(true);
    expect(document.activeElement).toBe(ui.opener);
  });

  it("ignores disabled controls when cycling focus", async () => {
    const ui = mountDialog();
    ui.name.disabled = true;
    a11y.openDialog(ui.dialog);

    const ids = a11y.focusableElements(ui.dialog).map((element) => element.id);
    expect(ids).toEqual(["close", "save"]);
  });
});
