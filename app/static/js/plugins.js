// AI Code Assistant — plugin management page
// Workspace-scoped plugin list, install, enable/disable, and explicit
// capability grants. The backend is authoritative for authorization.

(function () {
  "use strict";

  var WORKSPACE_ID = null;
  var IS_OWNER = false;

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
          error.status = response.status;
          throw error;
        }
        return data;
      });
    });
  }

  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function capabilityChips(caps) {
    if (!caps || !caps.length) return '<span class="activity-meta">none</span>';
    return caps.map(function (c) {
      return '<span class="cap-chip">' + escapeHtml(c) + "</span>";
    }).join("");
  }

  function pluginRow(plugin) {
    var stateBadge;
    if (!plugin.installed) {
      stateBadge = '<span class="project-status project-status-pending">not installed</span>';
    } else if (plugin.enabled) {
      stateBadge = '<span class="project-status project-status-ready">enabled</span>';
    } else {
      stateBadge = '<span class="project-status project-status-error">disabled</span>';
    }

    var actions = "";
    if (IS_OWNER) {
      if (plugin.installed) {
        if (!plugin.enabled) {
          actions += '<button class="btn btn-ghost btn-sm plugin-enable" data-id="' + escapeHtml(plugin.id) + '" type="button">Enable</button> ';
        } else {
          actions += '<button class="btn btn-ghost btn-sm plugin-disable" data-id="' + escapeHtml(plugin.id) + '" type="button">Disable</button> ';
        }
        actions +=
          '<span class="activity-meta" style="margin-left:4px;">granted: ' +
          capabilityChips(plugin.granted_capabilities) +
          "</span>";
      } else {
        actions = '<span class="activity-meta">installed in another workspace or not yet installed</span>';
      }
    } else {
      actions = '<span class="activity-meta">owner can manage</span>';
    }

    return (
      '<div class="project-row plugin-row" data-id="' + escapeHtml(plugin.id) + '">' +
      "<div>" +
      '<div class="project-name">' + escapeHtml(plugin.name) +
      ' <span class="activity-meta">v' + escapeHtml(plugin.version) + "</span> " + stateBadge + "</div>" +
      "<p class=\"project-meta\">" + escapeHtml(plugin.description || "No description.") + "</p>" +
      "<p class=\"project-meta\">author: " + escapeHtml(plugin.author || "unknown") +
      " &middot; id: " + escapeHtml(plugin.id) + "</p>" +
      '<div class="activity-meta" style="margin-bottom:4px;">declared capabilities: ' +
      capabilityChips(plugin.declared_capabilities) + "</div>" +
      "</div>" +
      '<div class="plugin-actions">' + actions + "</div>" +
      "</div>"
    );
  }

  function renderCapabilityEditor(plugin) {
    if (!IS_OWNER || !plugin.installed) return "";
    var declared = plugin.declared_capabilities || [];
    var granted = plugin.granted_capabilities || [];
    var boxes = declared.map(function (cap) {
      var checked = granted.indexOf(cap) !== -1 ? " checked" : "";
      return (
        '<label class="cap-check"><input type="checkbox" data-cap="' + escapeHtml(cap) + '"' + checked + "> " +
        escapeHtml(cap) + "</label>"
      );
    }).join("");
    if (!boxes) return "";
    return (
      '<div class="cap-editor" data-plugin="' + escapeHtml(plugin.id) + '">' +
      '<div class="cap-editor-boxes">' + boxes + "</div>" +
      '<button class="btn btn-ghost btn-sm cap-save" type="button">Save capabilities</button>' +
      "</div>"
    );
  }

  function renderPlugins(data) {
    var list = document.getElementById("plugin-list");
    if (!data.plugins.length) {
      list.innerHTML = '<p class="empty-note">No plugins registered. An owner can install a plugin above.</p>';
      return;
    }
    var html = data.plugins.map(function (p) {
      return pluginRow(p) + (p.installed ? renderCapabilityEditor(p) : "");
    }).join("");
    list.innerHTML = html;

    if (IS_OWNER) {
      list.querySelectorAll(".plugin-enable").forEach(function (btn) {
        btn.addEventListener("click", function () { setPluginEnabled(btn.dataset.id, true); });
      });
      list.querySelectorAll(".plugin-disable").forEach(function (btn) {
        btn.addEventListener("click", function () { setPluginEnabled(btn.dataset.id, false); });
      });
      list.querySelectorAll(".cap-save").forEach(function (btn) {
        btn.addEventListener("click", function () { saveCapabilities(btn); });
      });
    }
  }

  function setPluginEnabled(pluginId, enabled) {
    var path = "/plugins/api/workspaces/" + WORKSPACE_ID + "/plugins/" + encodeURIComponent(pluginId) +
      (enabled ? "/enable" : "/disable");
    api(path, { method: "POST" })
      .then(function () { loadPlugins(); })
      .catch(function (error) { flash(error.message, "error"); });
  }

  function saveCapabilities(button) {
    var editor = button.closest(".cap-editor");
    var pluginId = editor.dataset.plugin;
    var grant = [];
    var revoke = [];
    editor.querySelectorAll('input[type="checkbox"]').forEach(function (box) {
      var cap = box.dataset.cap;
      if (box.checked) grant.push(cap);
      else revoke.push(cap);
    });
    api("/plugins/api/workspaces/" + WORKSPACE_ID + "/plugins/" + encodeURIComponent(pluginId) + "/capabilities", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ grant: grant, revoke: revoke }),
    })
      .then(function () { flash("Capabilities updated.", "success"); loadPlugins(); })
      .catch(function (error) { flash(error.message, "error"); });
  }

  function loadPlugins() {
    var list = document.getElementById("plugin-list");
    list.innerHTML = '<p class="empty-note">Loading plugins…</p>';
    api("/plugins/api/workspaces/" + WORKSPACE_ID + "/plugins")
      .then(renderPlugins)
      .catch(function (error) {
        list.innerHTML = '<p class="empty-note">' + escapeHtml(error.message) + "</p>";
      });
  }

  function setWorkspace(workspaceId) {
    WORKSPACE_ID = workspaceId;
    var page = document.getElementById("plugin-page");
    var empty = document.getElementById("plugin-empty");
    var message = document.getElementById("plugin-workspace-message");
    var installBox = document.getElementById("plugin-install-box");
    var banner = document.getElementById("plugin-role-banner");
    var status = document.getElementById("plugin-install-status");

    if (!workspaceId) {
      page.hidden = true;
      empty.hidden = false;
      return;
    }

    api("/plugins/api/workspaces").then(function (workspaces) {
      var ws = workspaces.find(function (w) { return String(w.id) === String(workspaceId); });
      IS_OWNER = Boolean(ws && ws.role === "owner");
      banner.textContent = "Your role in this workspace: " + (ws ? ws.role : "unknown");
      installBox.hidden = !IS_OWNER;
      if (status) status.hidden = true;
      page.hidden = false;
      empty.hidden = true;
      loadPlugins();
    }).catch(function () {
      page.hidden = true;
      empty.hidden = false;
      message.hidden = false;
      message.textContent = "You cannot manage plugins for this workspace.";
    });
  }

  function installPlugin() {
    var input = document.getElementById("plugin-manifest-input");
    var btn = document.getElementById("plugin-install-btn");
    var status = document.getElementById("plugin-install-status");
    var raw = (input.value || "").trim();
    if (!raw) {
      flash("Paste a plugin manifest to install.", "warning");
      return;
    }
    var manifest;
    try {
      manifest = JSON.parse(raw);
    } catch (e) {
      flash("Manifest is not valid JSON.", "error");
      return;
    }
    btn.disabled = true;
    status.hidden = false;
    status.textContent = "Validating manifest…";
    api("/plugins/api/workspaces/" + WORKSPACE_ID + "/plugins/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ manifest: manifest }),
    })
      .then(function () {
        flash("Plugin installed.", "success");
        input.value = "";
        status.hidden = true;
        loadPlugins();
      })
      .catch(function (error) {
        status.hidden = true;
        flash(error.message, "error");
      })
      .finally(function () { btn.disabled = false; });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var select = document.getElementById("plugin-workspace-select");
    select.addEventListener("change", function () { setWorkspace(select.value); });
    document.getElementById("plugin-install-btn").addEventListener("click", installPlugin);
  });
})();
