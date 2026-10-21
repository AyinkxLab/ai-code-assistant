// AI Code Assistant — plugin management page
// Workspace-scoped plugin list, install, enable/disable, and explicit
// capability grants. The backend is authoritative for authorization.

(function () {
  "use strict";

  var WORKSPACE_ID = null;
  var IS_OWNER = false;
  var MARKETPLACE = window.AICAPluginMarketplace || null;

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
    if (MARKETPLACE && typeof MARKETPLACE.fetchJson === "function") {
      return MARKETPLACE.fetchJson(url, options);
    }
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
    if (MARKETPLACE && typeof MARKETPLACE.escapeHtml === "function") {
      return MARKETPLACE.escapeHtml(text);
    }
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function showPanelState(container, kind, options, onRetry) {
    if (!container) return;
    if (MARKETPLACE && typeof MARKETPLACE.stateHtml === "function") {
      container.innerHTML = MARKETPLACE.stateHtml(kind, options);
    } else {
      container.innerHTML = '<p class="empty-note">' + escapeHtml((options && options.message) || "") + "</p>";
    }
    if (onRetry) {
      if (MARKETPLACE && typeof MARKETPLACE.bindRetry === "function") {
        MARKETPLACE.bindRetry(container, onRetry);
      } else {
        var button = container.querySelector(".marketplace-retry");
        if (button) button.addEventListener("click", onRetry);
      }
    }
  }

  function showPanelError(container, error, options, onRetry) {
    if (!container) return;
    if (MARKETPLACE && typeof MARKETPLACE.errorStateHtml === "function") {
      container.innerHTML = MARKETPLACE.errorStateHtml(error, options);
      if (onRetry && typeof MARKETPLACE.bindRetry === "function") {
        MARKETPLACE.bindRetry(container, onRetry);
      }
    } else {
      showPanelState(container, "error", options || {}, onRetry);
    }
    if (error && error.code === "timeout") {
      flash("The marketplace request timed out. Please try again.", "warning");
    }
  }

  function capabilityChips(caps) {
    if (!caps || !caps.length) return '<span class="activity-meta">none</span>';
    return caps.map(function (c) {
      return '<span class="cap-chip">' + escapeHtml(c) + "</span>";
    }).join("");
  }

  function pluginBadges(plugin) {
    if (
      typeof window !== "undefined" &&
      window.AICAPluginBadges &&
      typeof window.AICAPluginBadges.render === "function"
    ) {
      return " " + window.AICAPluginBadges.render(plugin);
    }
    return "";
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

    var actions =
      '<a class="btn btn-ghost btn-sm" href="/plugins/workspaces/' + WORKSPACE_ID +
      "/plugins/" + encodeURIComponent(plugin.id) + '">Details</a> ';
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
        actions += '<span class="activity-meta">installed in another workspace or not yet installed</span>';
      }
    } else {
      actions += '<span class="activity-meta">owner can manage</span>';
    }

    return (
      '<div class="project-row plugin-row" data-id="' + escapeHtml(plugin.id) + '">' +
      "<div>" +
      '<div class="project-name">' + escapeHtml(plugin.name) +
      ' <span class="activity-meta">v' + escapeHtml(plugin.version) + "</span> " + stateBadge +
      pluginBadges(plugin) + "</div>" +
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
      showPanelState(list, "empty", {
        title: "No plugins available yet",
        message: "An owner can install a plugin above, or install one from the catalog.",
      });
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
    showPanelState(list, "loading", { message: "Loading plugins…" });
    api("/plugins/api/workspaces/" + WORKSPACE_ID + "/plugins")
      .then(renderPlugins)
      .catch(function (error) {
        showPanelError(list, error, {
          title: "Could not load plugins",
          message:
            error && error.code === "timeout"
              ? "The plugin list did not respond in time. Please try again."
              : "We couldn’t load the plugins for this workspace. Please try again.",
          retry: true,
        }, loadPlugins);
      });
  }

  function loadLockfile() {
    var section = document.getElementById("plugin-lockfile-section");
    var container = document.getElementById("plugin-lockfile");
    if (!section || !container) return;

    section.hidden = false;
    if (!WORKSPACE_ID) return;

    showPanelState(container, "loading", { message: "Loading lockfile…" });
    api("/plugins/api/workspaces/" + WORKSPACE_ID + "/lockfile")
      .then(function (data) {
        if (MARKETPLACE && typeof MARKETPLACE.renderLockfile === "function") {
          container.innerHTML = MARKETPLACE.renderLockfile(data);
        } else {
          container.innerHTML = '<p class="empty-note">Lockfile unavailable.</p>';
        }
      })
      .catch(function (error) {
        showPanelError(container, error, {
          title: "Could not load the lockfile",
          message:
            error && error.code === "timeout"
              ? "The lockfile did not respond in time. Please try again."
              : "We couldn’t load the resolved plugin lockfile. Please try again.",
          retry: true,
        }, loadLockfile);
      });
  }

  function loadCatalog() {
    if (!window.AICAPluginCatalog || typeof window.AICAPluginCatalog.loadCatalog !== "function") {
      return;
    }
    window.AICAPluginCatalog.loadCatalog(WORKSPACE_ID, {
      canManage: IS_OWNER,
      onChanged: function () {
        loadPlugins();
        loadLockfile();
      },
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
    var lockfileSection = document.getElementById("plugin-lockfile-section");
    var marketplaceSection = document.getElementById("plugin-marketplace-section");

    if (!workspaceId) {
      page.hidden = true;
      empty.hidden = false;
      if (lockfileSection) lockfileSection.hidden = true;
      if (marketplaceSection) marketplaceSection.hidden = true;
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
      message.hidden = true;
      loadPlugins();
      loadLockfile();
      loadCatalog();
    }).catch(function (error) {
      page.hidden = false;
      empty.hidden = true;
      IS_OWNER = false;
      installBox.hidden = true;
      if (lockfileSection) lockfileSection.hidden = true;
      if (marketplaceSection) marketplaceSection.hidden = true;
      showPanelError(document.getElementById("plugin-list"), error, {
        title: "Could not open this workspace",
        message:
          error && error.code === "timeout"
            ? "Loading the workspace timed out. Please try again."
            : "We couldn’t load your role for this workspace. Please try again.",
        retry: true,
      }, function () { setWorkspace(workspaceId); });
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
    if (MARKETPLACE && typeof MARKETPLACE.busyOn === "function") {
      MARKETPLACE.busyOn(status, "Installing and validating the manifest\u2026");
    } else {
      status.hidden = false;
      status.textContent = "Installing and validating the manifest\u2026";
    }
    api("/plugins/api/workspaces/" + WORKSPACE_ID + "/plugins/install", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ manifest: manifest }),
    })
      .then(function () {
        flash("Plugin installed.", "success");
        input.value = "";
        if (MARKETPLACE && typeof MARKETPLACE.busyOff === "function") {
          MARKETPLACE.busyOff(status);
        } else {
          status.hidden = true;
        }
        loadPlugins();
      })
      .catch(function (error) {
        if (MARKETPLACE && typeof MARKETPLACE.busyError === "function") {
          MARKETPLACE.busyError(status, error);
        } else {
          status.hidden = true;
          flash(error.message, "error");
        }
      })
      .finally(function () { btn.disabled = false; });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var select = document.getElementById("plugin-workspace-select");
    select.addEventListener("change", function () { setWorkspace(select.value); });
    document.getElementById("plugin-install-btn").addEventListener("click", installPlugin);
  });
})();
