// AI Code Assistant — workspace audit log (owner only)
// Filters and paginates the security-relevant audit subset.

(function () {
  "use strict";

  var WORKSPACE_ID = parseInt(window.COLLAB_WORKSPACE_ID, 10) || 0;
  var PAGE = 1;
  var PER_PAGE = 20;

  function getCsrf() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.content;
    var input = document.querySelector('input[name="csrf_token"]');
    return input ? input.value : "";
  }

  function flash(message, category) {
    var stack = document.querySelector(".flash-stack");
    if (!stack) {
      stack = document.createElement("div");
      stack.className = "flash-stack";
      var main = document.querySelector(".main-content");
      (main || document.body).prepend(stack);
    }
    var el = document.createElement("div");
    el.className = "flash flash-" + (category || "info");
    el.textContent = message;
    stack.appendChild(el);
    setTimeout(function () { el.remove(); }, 6000);
  }

  function api(url, options) {
    options = options || {};
    options.headers = Object.assign({}, options.headers || {}, {
      "X-CSRFToken": getCsrf(),
    });
    return fetch(url, options).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) {
          var error = new Error(data && data.error ? data.error : "Request failed (" + response.status + ").");
          throw error;
        }
        return data;
      });
    });
  }

  function escapeHtml(value) {
    var div = document.createElement("div");
    div.textContent = value == null ? "" : String(value);
    return div.innerHTML;
  }

  function formatTime(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleString();
  }

  function buildQuery() {
    var params = ["page=" + PAGE, "per_page=" + PER_PAGE];
    var type = document.getElementById("audit-type").value;
    var actor = document.getElementById("audit-actor").value.trim();
    if (type) params.push("event_type=" + encodeURIComponent(type));
    if (actor) params.push("actor=" + encodeURIComponent(actor));
    return params.join("&");
  }

  function renderMetadata(meta) {
    if (!meta) return "";
    var parts = [];
    if (meta.email) parts.push(meta.email);
    if (meta.role) parts.push("role: " + meta.role);
    if (meta.target_username) parts.push("target: " + meta.target_username);
    if (meta.action && meta.action !== "changed") parts.push("role " + meta.action);
    if (meta.old_role && meta.new_role) parts.push(meta.old_role + " -> " + meta.new_role);
    else if (meta.new_role) parts.push("role: " + meta.new_role);
    else if (meta.old_role) parts.push("role removed: " + meta.old_role);
    if (meta.request_id) parts.push("request: " + meta.request_id);
    if (meta.from_username && meta.to_username) parts.push(meta.from_username + " -> " + meta.to_username);
    if (meta.reactivated) parts.push("reactivated");
    if (meta.idempotent) parts.push("idempotent");
    if (meta.plugin_id) parts.push("plugin: " + meta.plugin_id);
    if (meta.version) parts.push("v" + meta.version);
    if (meta.capability) parts.push("capability: " + meta.capability);
    if (meta.outcome) parts.push("outcome: " + meta.outcome);
    if (meta.duration_ms != null) parts.push(meta.duration_ms + " ms");
    return parts.length ? '<div class="audit-metadata">' + escapeHtml(parts.join(" &middot; ")) + "</div>" : "";
  }

  function loadAudit() {
    var list = document.getElementById("audit-list");
    api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/audit?" + buildQuery())
      .then(function (data) {
        if (!data.items.length) {
          list.innerHTML = '<p class="empty-note">No audit events match.</p>';
          return;
        }
        var html = "";
        data.items.forEach(function (e) {
          html +=
            '<div class="audit-row">' +
            '<span class="activity-dot"></span>' +
            '<div class="audit-body">' +
            '<div><span class="audit-actor">' + escapeHtml(e.actor_username || "system") + "</span> " + escapeHtml(e.label) + "</div>" +
            renderMetadata(e.metadata) +
            "</div>" +
            '<div class="audit-time">' + formatTime(e.created_at) + "</div>" +
            "</div>";
        });
        var totalPages = Math.max(1, Math.ceil(data.total / data.per_page));
        html +=
          '<div class="pager">' +
          '<button id="prev-page" class="btn btn-ghost btn-sm" type="button"' + (data.page <= 1 ? " disabled" : "") + ">Previous</button>" +
          '<span class="field-hint">Page ' + data.page + " of " + totalPages + " (" + data.total + " events)</span>" +
          '<button id="next-page" class="btn btn-ghost btn-sm" type="button"' + (data.page >= totalPages ? " disabled" : "") + ">Next</button>" +
          "</div>";
        list.innerHTML = html;
        document.getElementById("prev-page").addEventListener("click", function () {
          PAGE -= 1;
          loadAudit();
        });
        document.getElementById("next-page").addEventListener("click", function () {
          PAGE += 1;
          loadAudit();
        });
      })
      .catch(function (error) {
        list.innerHTML = '<p class="empty-note">' + escapeHtml(error.message) + "</p>";
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    loadAudit();
    document.getElementById("audit-filter").addEventListener("click", function () {
      PAGE = 1;
      loadAudit();
    });
    document.getElementById("audit-actor").addEventListener("keydown", function (event) {
      if (event.key === "Enter") {
        PAGE = 1;
        loadAudit();
      }
    });
  });
})();
