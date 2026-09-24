// AI Code Assistant — LLM provider API key settings scaffold.
// Lists redacted keys and adds/deletes them; never renders the secret value.

(function () {
  "use strict";

  var GH = window.GitHub;
  var listEl = document.getElementById("key-list");

  function render(keys) {
    listEl.innerHTML = "";
    if (!keys.length) {
      listEl.innerHTML = '<p class="sidebar-empty">No API keys stored yet.</p>';
      return;
    }
    keys.forEach(function (key) {
      var row = document.createElement("div");
      row.className = "issue-item";
      row.innerHTML =
        '<div class="issue-item-title">' + GH.escapeHtml(key.provider) +
        ' <span class="tag">' + (key.is_active ? "active" : "inactive") + "</span></div>" +
        '<div class="issue-item-sub">' + GH.escapeHtml(key.label || "no label") +
        " &middot; stored " + GH.relativeDate(key.created_at) + "</div>" +
        '<button class="btn btn-danger btn-sm" data-id="' + key.id + '" type="button">Delete</button>';
      listEl.appendChild(row);
    });
  }

  function refresh() {
    GH.api("/keys/api/keys").then(render).catch(function (error) {
      listEl.innerHTML = '<p class="sidebar-empty">Could not load API keys.</p>';
      GH.flashError(error.message);
    });
  }

  function add() {
    var provider = document.getElementById("key-provider").value;
    var label = document.getElementById("key-label").value.trim();
    var secret = document.getElementById("key-secret").value.trim();
    if (!secret) {
      GH.flashError("Enter the API key value.");
      return;
    }
    var button = document.getElementById("add-key");
    button.disabled = true;
    GH.api("/keys/api/keys", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ provider: provider, key: secret, label: label }),
    })
      .then(function () {
        document.getElementById("key-secret").value = "";
        document.getElementById("key-label").value = "";
        refresh();
      })
      .catch(function (error) {
        GH.flashError(error.message);
      })
      .then(function () {
        button.disabled = false;
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!listEl) return;
    refresh();
    document.getElementById("add-key").addEventListener("click", add);
    listEl.addEventListener("click", function (event) {
      var button = event.target.closest("button[data-id]");
      if (!button) return;
      if (!window.confirm("Delete this API key?")) return;
      GH.api("/keys/api/keys/" + button.dataset.id, { method: "DELETE" })
        .then(refresh)
        .catch(function (error) {
          GH.flashError(error.message);
        });
    });
  });
})();
