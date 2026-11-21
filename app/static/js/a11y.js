// Keyboard navigation helpers (issue #713).
//
// Dialogs in this app are plain markup toggled with the `hidden` attribute by
// feature scripts. This module adds the behaviour those scripts should not each
// reimplement: focus moves into a dialog when it opens, Tab/Shift+Tab cycle
// inside it, Escape closes it, and focus returns to the control that opened it.
// No dependencies and no framework: progressive enhancement only.

(function () {
  "use strict";

  var FOCUSABLE =
    'a[href], button:not([disabled]), input:not([disabled]):not([type="hidden"]), ' +
    'select:not([disabled]), textarea:not([disabled]), [tabindex]:not([tabindex="-1"])';

  var lastTrigger = null;

  function isVisible(element) {
    return !element.hidden && element.getAttribute("aria-hidden") !== "true";
  }

  function focusableElements(root) {
    return Array.prototype.filter.call(root.querySelectorAll(FOCUSABLE), isVisible);
  }

  // Keep Tab inside the dialog. Escaping focus from a modal is a keyboard trap,
  // so both ends wrap instead of leaving the document.
  function trapTab(dialog, event) {
    var items = focusableElements(dialog);
    if (!items.length) {
      event.preventDefault();
      return;
    }
    var first = items[0];
    var last = items[items.length - 1];
    var active = document.activeElement;
    var inside = dialog.contains(active);

    if (event.shiftKey) {
      if (!inside || active === first || active === dialog) {
        event.preventDefault();
        last.focus();
      }
    } else if (!inside || active === last) {
      event.preventDefault();
      first.focus();
    }
  }

  function openDialog(dialog) {
    if (!dialog) return;
    if (lastTrigger && !dialog.contains(lastTrigger)) {
      dialog.__aicaTrigger = lastTrigger;
    }
    // The mutation observer calls this after the feature script has already
    // cleared `hidden`; direct callers call it before. Both must focus.
    if (dialog.hidden) dialog.hidden = false;
    var items = focusableElements(dialog);
    var target = items.length ? items[0] : dialog;
    if (target === dialog) dialog.setAttribute("tabindex", "-1");
    target.focus();
  }

  function closeDialog(dialog) {
    if (!dialog || dialog.hidden) return;
    dialog.hidden = true;
    var trigger = dialog.__aicaTrigger;
    if (trigger && document.contains(trigger)) {
      trigger.focus();
    }
    dialog.__aicaTrigger = null;
  }

  function enhanceDialog(dialog) {
    if (dialog.__aicaReady) return;
    dialog.__aicaReady = true;
    var wasHidden = dialog.hidden;

    var observer = new MutationObserver(function () {
      if (dialog.hidden === wasHidden) return;
      wasHidden = dialog.hidden;
      if (dialog.hidden) {
        closeDialog(dialog);
      } else {
        openDialog(dialog);
      }
    });
    observer.observe(dialog, { attributes: true, attributeFilter: ["hidden"] });

    dialog.addEventListener("keydown", function (event) {
      if (event.key === "Escape") {
        event.preventDefault();
        closeDialog(dialog);
      } else if (event.key === "Tab") {
        trapTab(dialog, event);
      }
    });
  }

  function init() {
    // Remember what was focused before a click opened a dialog, so focus can
    // return there when the dialog closes. Capture phase: the dialog's own
    // click handler runs later.
    document.addEventListener(
      "click",
      function (event) {
        var trigger = event.target.closest && event.target.closest('button, a, [role="button"]');
        if (trigger) lastTrigger = trigger;
      },
      true
    );
    Array.prototype.forEach.call(
      document.querySelectorAll(".modal, [role='dialog']"),
      enhanceDialog
    );
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", init);
  } else {
    init();
  }

  window.AICA11y = {
    focusableElements: focusableElements,
    trapTab: trapTab,
    openDialog: openDialog,
    closeDialog: closeDialog,
    init: init,
  };
})();
