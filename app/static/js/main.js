// AI Code Assistant — client-side helpers
// Phase 1: minimal, progressive enhancement. Future phases add the editor,
// chat UI, and real-time features here.

(function () {
  "use strict";

  // Auto-dismiss flash messages after a short delay.
  document.addEventListener("DOMContentLoaded", function () {
    var flashes = document.querySelectorAll(".flash");
    flashes.forEach(function (flash) {
      setTimeout(function () {
        flash.style.transition = "opacity .4s ease";
        flash.style.opacity = "0";
        setTimeout(function () {
          flash.remove();
        }, 400);
      }, 4000);
    });
  });

  // Refresh the header notification badge (Phase 7 #156).
  function refreshNotificationBadge() {
    var badge = document.getElementById("notif-badge");
    if (!badge) return;
    fetch("/workspaces/api/notifications/count", { credentials: "same-origin" })
      .then(function (response) { return response.json(); })
      .then(function (data) {
        var count = data && data.unread ? data.unread : 0;
        badge.hidden = count === 0;
        badge.textContent = count > 99 ? "99+" : String(count);
      })
      .catch(function () {
        // The count is a progressive enhancement; ignore failures.
      });
  }

  document.addEventListener("DOMContentLoaded", refreshNotificationBadge);
  setInterval(refreshNotificationBadge, 60000);

  // ------------------------------------------------------------------------
  // File explorer keyboard navigation.
  //
  // Arrow keys move the focus within the tree, Enter opens the focused
  // file, and Esc jumps to the parent directory. The handler is bound to
  // the tree container so it only runs when the explorer has focus.
  // ------------------------------------------------------------------------

  var FOCUSED_CLASS = "is-focused";

  function getTreeRoot() {
    return document.getElementById("project-tree");
  }

  function getFocusableRows() {
    var root = getTreeRoot();
    if (!root) return [];
    return Array.prototype.slice.call(
      root.querySelectorAll("[data-tree-row-index]")
    );
  }

  function getFocusedIndex() {
    var root = getTreeRoot();
    if (!root) return -1;
    var row = root.querySelector("." + FOCUSED_CLASS);
    if (!row) return -1;
    var index = parseInt(row.getAttribute("data-tree-row-index"), 10);
    return isNaN(index) ? -1 : index;
  }

  function setFocusedRow(row) {
    var root = getTreeRoot();
    if (!root || !row) return;
    var previous = root.querySelectorAll("." + FOCUSED_CLASS);
    previous.forEach(function (el) {
      el.classList.remove(FOCUSED_CLASS);
      el.setAttribute("aria-selected", "false");
    });
    row.classList.add(FOCUSED_CLASS);
    row.setAttribute("aria-selected", "true");
    if (typeof row.scrollIntoView === "function") {
      row.scrollIntoView({ block: "nearest" });
    }
  }

  function focusRowByIndex(index) {
    var rows = getFocusableRows();
    if (!rows.length) return null;
    var clamped = Math.max(0, Math.min(rows.length - 1, index));
    var row = rows[clamped];
    setFocusedRow(row);
    return row;
  }

  function activateRow(row) {
    if (!row) return;
    var target = row.querySelector("[data-tree-action]") || row;
    if (typeof target.click === "function") {
      target.click();
    }
  }

  function parentPath(path) {
    if (!path) return "";
    var trimmed = path.replace(/\/+$/, "");
    var idx = trimmed.lastIndexOf("/");
    if (idx <= 0) return "";
    return trimmed.slice(0, idx);
  }

  function goToParent() {
    var root = getTreeRoot();
    if (!root) return;
    var focused = root.querySelector("." + FOCUSED_CLASS);
    var path = focused ? focused.getAttribute("data-tree-path") : "";
    var parent = parentPath(path);
    var rows = getFocusableRows();
    if (!rows.length) return null;
    if (!parent) {
      // Already at the root: focus the first row.
      return focusRowByIndex(0);
    }
    for (var i = 0; i < rows.length; i++) {
      if (rows[i].getAttribute("data-tree-path") === parent) {
        setFocusedRow(rows[i]);
        return rows[i];
      }
    }
    // Parent not rendered yet: fall back to the first row.
    return focusRowByIndex(0);
  }

  function onKeydown(event) {
    var root = getTreeRoot();
    if (!root) return;
    // Only react when focus is inside the explorer (or on the container).
    if (!root.contains(document.activeElement) && document.activeElement !== root) return;
    var rows = getFocusableRows();
    if (!rows.length) return;
    var index = getFocusedIndex();
    var handled = true;
    switch (event.key) {
      case "ArrowDown":
        focusRowByIndex(index < 0 ? 0 : index + 1);
        break;
      case "ArrowUp":
        focusRowByIndex(index < 0 ? 0 : index - 1);
        break;
      case "Home":
        focusRowByIndex(0);
        break;
      case "End":
        focusRowByIndex(rows.length - 1);
        break;
      case "Enter": {
        var focused = root.querySelector("." + FOCUSED_CLASS);
        activateRow(focused);
        break;
      }
      case "Escape":
        goToParent();
        break;
      default:
        handled = false;
    }
    if (handled) {
      event.preventDefault();
      event.stopPropagation();
    }
  }

  function initFileExplorerNavigation() {
    var root = getTreeRoot();
    if (!root) return;
    if (root.getAttribute("data-keyboard-bound") === "true") return;
    root.setAttribute("data-keyboard-bound", "true");
    root.setAttribute("tabindex", "0");
    root.setAttribute("role", "tree");
    root.addEventListener("keydown", onKeydown);
    // Keep the focus state in sync when the user clicks a row.
    root.addEventListener("click", function (event) {
      var row = event.target.closest("[data-tree-row-index]");
      if (row && root.contains(row)) {
        setFocusedRow(row);
      }
    });
  }

  document.addEventListener("DOMContentLoaded", initFileExplorerNavigation);

  // Expose a small hook so the tree renderer can re-attach after a refresh.
  window.fileExplorerNavigation = {
    init: initFileExplorerNavigation,
    focusRowByIndex: focusRowByIndex,
    setFocusedRow: setFocusedRow,
    getFocusedIndex: getFocusedIndex
  };
})();
