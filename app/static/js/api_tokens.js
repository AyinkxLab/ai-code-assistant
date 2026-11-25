// AI Code Assistant — personal API token management.
// Lists metadata only; a secret is rendered once after create/rotate.

(function () {
  "use strict";

  var GH = window.GitHub;
  var listEl = document.getElementById("token-list");

  function showSecret(secret) {
    var panel = document.getElementById("token-secret");
    document.getElementById("token-secret-value").textContent = secret;
    panel.hidden = false;
  }

  function status(token) {
    if (token.revoked_at) return "revoked";
    if (token.is_active) return "active";
    return "expired";
  }

  function render(tokens) {
    listEl.innerHTML = "";
    if (!tokens.length) {
      listEl.innerHTML = '<p class="sidebar-empty">No API tokens yet.</p>';
      return;
    }
    tokens.forEach(function (token) {
      var row = document.createElement("div");
      row.className = "issue-item";
      row.innerHTML =
        '<div class="issue-item-title">' + GH.escapeHtml(token.label || "no label") +
        " <span class=\"tag\">" + status(token) + "</span></div>" +
        '<div class="issue-item-sub">' + GH.escapeHtml(token.prefix) + "&hellip;" +
        " &middot; " + GH.escapeHtml(token.scopes.join(", ")) +
        " &middot; created " + GH.relativeDate(token.created_at) +
        " &middot; last used " + (token.last_used_at ? GH.relativeDate(token.last_used_at) : "never") +
        " &middot; expires " + (token.expires_at ? GH.relativeDate(token.expires_at) : "never") +
        "</div>" +
        (token.is_active
          ? '<button class="btn btn-sm" data-action="rotate" data-id="' + token.id +
            '" type="button">Rotate</button> ' +
            '<button class="btn btn-danger btn-sm" data-action="revoke" data-id="' + token.id +
            '" type="button">Revoke</button>'
          : "");
      listEl.appendChild(row);
    });
  }

  function refresh() {
    GH.api("/settings/tokens/api")
      .then(render)
      .catch(function (error) {
        listEl.innerHTML = '<p class="sidebar-empty">Could not load API tokens.</p>';
        GH.flashError(error.message);
      });
  }

  function scopes() {
    return Array.prototype.map.call(
      document.querySelectorAll("input[data-scope]:checked"),
      function (input) {
        return input.dataset.scope;
      }
    );
  }

  function create() {
    var label = document.getElementById("token-label").value.trim();
    var button = document.getElementById("add-token");
    button.disabled = true;
    GH.api("/settings/tokens/api", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ label: label, scopes: scopes() }),
    })
      .then(function (body) {
        document.getElementById("token-label").value = "";
        showSecret(body.secret);
        refresh();
      })
      .catch(function (error) {
        GH.flashError(error.message);
      })
      .then(function () {
        button.disabled = false;
      });
  }

  listEl.addEventListener("click", function (event) {
    var button = event.target.closest("button[data-action]");
    if (!button) return;
    var id = button.dataset.id;
    if (button.dataset.action === "rotate") {
      if (!window.confirm("Rotate this token? The current secret will stop working.")) return;
      GH.api("/settings/tokens/api/" + id + "/rotate", { method: "POST" })
        .then(function (body) {
          showSecret(body.secret);
          refresh();
        })
        .catch(function (error) {
          GH.flashError(error.message);
        });
      return;
    }
    if (!window.confirm("Revoke this token? This cannot be undone.")) return;
    GH.api("/settings/tokens/api/" + id, { method: "DELETE" })
      .then(refresh)
      .catch(function (error) {
        GH.flashError(error.message);
      });
  });

  document.addEventListener("DOMContentLoaded", function () {
    if (!listEl) return;
    refresh();
    document.getElementById("add-token").addEventListener("click", create);
  });
})();
