// AI Code Assistant - marketplace catalog browser (#364)
//
// Members can browse the operator-controlled catalog (trust, compatibility,
// dependencies, artifact integrity and install/update/rollback state). Only
// owners see action buttons; the backend independently enforces authorization.
// All catalog text is escaped before it is written to the DOM.

(function (root) {
  "use strict";

  var MARKETPLACE = root.AICAPluginMarketplace || null;
  var PAGE_LIMIT = 50;
  var state = {
    workspaceId: null,
    page: 1,
    canManage: false,
    onChanged: null,
  };

  function escapeHtml(text) {
    if (MARKETPLACE && typeof MARKETPLACE.escapeHtml === "function") {
      return MARKETPLACE.escapeHtml(text);
    }
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  function safeSource(value) {
    if (MARKETPLACE && typeof MARKETPLACE.safeSource === "function") {
      return MARKETPLACE.safeSource(value);
    }
    return String(value == null ? "" : value);
  }

  function getCsrf() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.content;
    var input = document.querySelector('input[name="csrf_token"]');
    return input ? input.value : "";
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
          var error = new Error(
            data && data.error ? data.error : "Request failed (" + response.status + ")."
          );
          error.status = response.status;
          throw error;
        }
        return data;
      });
    });
  }

  function badges(entry) {
    if (root.AICAPluginBadges && typeof root.AICAPluginBadges.render === "function") {
      return root.AICAPluginBadges.render(entry);
    }
    return "";
  }

  function chips(values) {
    if (!Array.isArray(values) || !values.length) {
      return '<span class="activity-meta">none</span>';
    }
    return values
      .map(function (value) {
        return '<span class="cap-chip">' + escapeHtml(value) + "</span>";
      })
      .join(" ");
  }

  function dependencies(entry) {
    if (!Array.isArray(entry.dependencies) || !entry.dependencies.length) {
      return '<span class="activity-meta">none</span>';
    }
    return (
      '<ul class="plugin-dependencies">' +
      entry.dependencies
        .map(function (dependency) {
          return "<li><code>" + escapeHtml(dependency) + "</code></li>";
        })
        .join("") +
      "</ul>"
    );
  }

  function stateBadges(entry) {
    var parts = [];
    if (entry.installed) {
      parts.push(
        '<span class="project-status project-status-' +
          (entry.enabled ? "ready" : "error") +
          '">' +
          (entry.enabled ? "enabled" : "disabled") +
          " v" +
          escapeHtml(entry.installed_version || entry.version) +
          "</span>"
      );
    } else {
      parts.push('<span class="project-status project-status-pending">not installed</span>');
    }
    if (entry.update_available) {
      parts.push(
        '<span class="plugin-catalog-update">Update available: v' +
          escapeHtml(entry.update_version) +
          "</span>"
      );
    }
    if (entry.rollback_available) {
      parts.push(
        '<span class="plugin-catalog-rollback">Rollback target: v' +
          escapeHtml(entry.rollback_version) +
          "</span>"
      );
    }
    return parts.join(" ");
  }

  function actions(entry, canManage) {
    if (!canManage || !entry.actions) return "";
    var id = escapeHtml(entry.id);
    var version = escapeHtml(entry.version);
    var buttons = [];
    if (entry.actions.install) {
      buttons.push(
        '<button type="button" class="btn btn-primary btn-sm" data-catalog-action="install" ' +
          'data-plugin-id="' +
          id +
          '" data-version="' +
          version +
          '">Install v' +
          version +
          "</button>"
      );
    }
    if (entry.actions.update && entry.is_latest) {
      buttons.push(
        '<button type="button" class="btn btn-primary btn-sm" data-catalog-action="update" ' +
          'data-plugin-id="' +
          id +
          '" data-version="' +
          version +
          '">Update to v' +
          version +
          "</button>"
      );
    }
    if (entry.actions.rollback && entry.is_latest) {
      buttons.push(
        '<button type="button" class="btn btn-ghost btn-sm" data-catalog-action="rollback" ' +
          'data-plugin-id="' +
          id +
          '" data-version="' +
          escapeHtml(entry.rollback_version || "") +
          '">Rollback</button>'
      );
    }
    return buttons.length
      ? '<div class="plugin-catalog-actions">' + buttons.join(" ") + "</div>"
      : "";
  }

  function renderEntry(entry, options) {
    options = options || {};
    var canManage =
      typeof options.canManage === "boolean" ? options.canManage : Boolean(entry.can_manage);
    var description = entry.description || "No description.";
    var author = entry.author || "unknown";
    var artifact = entry.integrity || entry.artifact_sha256 || "";
    var source = safeSource(entry.source || (entry.artifact && entry.artifact.url) || "");

    return (
      '<article class="plugin-catalog-card" data-plugin-id="' +
      escapeHtml(entry.id) +
      '">' +
      '<div class="plugin-catalog-heading">' +
      '<div><h3 class="plugin-catalog-name">' +
      escapeHtml(entry.name || entry.id) +
      ' <span class="activity-meta">v' +
      escapeHtml(entry.version) +
      "</span></h3>" +
      '<p class="project-meta">author: ' +
      escapeHtml(author) +
      " &middot; id: " +
      escapeHtml(entry.id) +
      "</p></div>" +
      "<div>" +
      badges(entry) +
      "</div></div>" +
      '<p class="project-meta">' +
      escapeHtml(description) +
      "</p>" +
      '<p class="plugin-catalog-state">' +
      stateBadges(entry) +
      "</p>" +
      '<div class="plugin-catalog-meta">' +
      "<div><strong>Compatibility:</strong> " +
      escapeHtml(entry.compatibility || "any") +
      " &middot; " +
      (entry.compatible_with_app ? "compatible" : "incompatible") +
      " with app " +
      escapeHtml(entry.app_version || "") +
      "</div>" +
      "<div><strong>Capabilities:</strong> " +
      chips(entry.capabilities) +
      "</div>" +
      "<div><strong>Permissions:</strong> " +
      chips(entry.permissions) +
      "</div>" +
      "<div><strong>Dependencies:</strong> " +
      dependencies(entry) +
      "</div>" +
      "<div><strong>Artifact:</strong> " +
      (artifact
        ? '<code class="plugin-catalog-hash">' + escapeHtml(artifact) + "</code>"
        : '<span class="activity-meta">not recorded</span>') +
      "</div>" +
      "<div><strong>Source:</strong> " +
      (source
        ? '<span class="activity-meta">' + escapeHtml(source) + "</span>"
        : '<span class="activity-meta">unknown</span>') +
      "</div>" +
      "</div>" +
      actions(entry, canManage) +
      "</article>"
    );
  }

  function renderCatalog(data, options) {
    options = options || {};
    var entries = data && Array.isArray(data.plugins) ? data.plugins : [];
    if (!entries.length) {
      if (MARKETPLACE && typeof MARKETPLACE.stateHtml === "function") {
        return MARKETPLACE.stateHtml("empty", {
          title: "No catalog entries available",
          message:
            "The operator has not published any plugins in the marketplace catalog yet.",
        });
      }
      return '<p class="empty-note">No catalog entries available.</p>';
    }
    var canManage =
      typeof options.canManage === "boolean"
        ? options.canManage
        : Boolean(data.workspace && data.workspace.can_manage);
    return (
      '<div class="plugin-catalog-grid">' +
      entries
        .map(function (entry) {
          return renderEntry(entry, { canManage: canManage });
        })
        .join("") +
      "</div>"
    );
  }

  function paginationHtml(pagination) {
    if (!pagination) return "";
    var page = Number(pagination.page) || 1;
    var pages = Number(pagination.pages) || 0;
    if (!pages) return "";
    return (
      '<button type="button" class="btn btn-ghost btn-sm" data-catalog-page="' +
      (page - 1) +
      '"' +
      (pagination.has_prev ? "" : " disabled") +
      ">Previous</button> " +
      '<span class="activity-meta">Page ' +
      escapeHtml(page) +
      " of " +
      escapeHtml(pages) +
      " (" +
      escapeHtml(pagination.total) +
      " entries)</span> " +
      '<button type="button" class="btn btn-ghost btn-sm" data-catalog-page="' +
      (page + 1) +
      '"' +
      (pagination.has_next ? "" : " disabled") +
      ">Next</button>"
    );
  }

  function showMessage(text, category) {
    var el = document.getElementById("plugin-catalog-message");
    if (!el) return;
    el.hidden = !text;
    el.className = "flash flash-" + (category || "info");
    el.textContent = text || "";
  }

  function actionUrl(entry, action) {
    var base = "/plugins/api/workspaces/" + state.workspaceId + "/plugins/";
    var plugin = encodeURIComponent(entry.pluginId);
    if (action === "install") return base + "install-from-catalog";
    if (action === "update") return base + plugin + "/update-from-catalog";
    return base + plugin + "/rollback";
  }

  function runAction(button) {
    var action = button.getAttribute("data-catalog-action");
    var pluginId = button.getAttribute("data-plugin-id");
    var version = button.getAttribute("data-version");
    if (!action || !pluginId) return;
    if (action === "rollback" && !root.confirm("Roll back " + pluginId + "?")) return;

    var body = action === "install" ? { plugin_id: pluginId } : {};
    if (version && action !== "rollback") body.version = version;

    button.disabled = true;
    showMessage("Working\u2026", "info");
    api(actionUrl({ pluginId: pluginId }, action), {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    })
      .then(function () {
        showMessage(
          action === "install"
            ? "Plugin installed from catalog."
            : action === "update"
              ? "Plugin updated."
              : "Plugin rolled back.",
          "success"
        );
        if (typeof state.onChanged === "function") state.onChanged();
        loadCatalog(state.workspaceId, {
          page: state.page,
          canManage: state.canManage,
          onChanged: state.onChanged,
        });
      })
      .catch(function (error) {
        showMessage(
          error && error.code === "timeout"
            ? "The marketplace request timed out. Please try again."
            : error && error.message
              ? error.message
              : "The marketplace action failed. Please try again.",
          "error"
        );
      })
      .finally(function () {
        button.disabled = false;
      });
  }

  function bind(workspaceId, page, canManage, onChanged) {
    var container = document.getElementById("plugin-catalog");
    if (!container) return;
    container.querySelectorAll("[data-catalog-action]").forEach(function (button) {
      button.addEventListener("click", function () {
        runAction(button);
      });
    });
    var pager = document.getElementById("plugin-catalog-pagination");
    if (pager) {
      pager.querySelectorAll("[data-catalog-page]").forEach(function (button) {
        if (button.disabled) return;
        button.addEventListener("click", function () {
          loadCatalog(workspaceId, {
            page: Number(button.getAttribute("data-catalog-page")) || 1,
            canManage: canManage,
            onChanged: onChanged,
          });
        });
      });
    }
  }

  function currentFilters() {
    var search = document.getElementById("plugin-catalog-search");
    var capability = document.getElementById("plugin-catalog-capability");
    return {
      q: search ? String(search.value || "").trim() : "",
      capability: capability ? String(capability.value || "") : "",
    };
  }

  function bindFilters() {
    var search = document.getElementById("plugin-catalog-search");
    if (search && !search.dataset.bound) {
      search.dataset.bound = "1";
      var timer = null;
      search.addEventListener("input", function () {
        window.clearTimeout(timer);
        timer = window.setTimeout(function () {
          loadCatalog(state.workspaceId, {
            page: 1,
            canManage: state.canManage,
            onChanged: state.onChanged,
          });
        }, 250);
      });
    }
    var capability = document.getElementById("plugin-catalog-capability");
    if (capability && !capability.dataset.bound) {
      capability.dataset.bound = "1";
      capability.addEventListener("change", function () {
        loadCatalog(state.workspaceId, {
          page: 1,
          canManage: state.canManage,
          onChanged: state.onChanged,
        });
      });
    }
  }

  function loadCatalog(workspaceId, options) {
    options = options || {};
    state.workspaceId = workspaceId || null;
    state.page = options.page || 1;
    state.canManage = Boolean(options.canManage);
    state.onChanged = options.onChanged || state.onChanged;

    var section = document.getElementById("plugin-marketplace-section");
    var container = document.getElementById("plugin-catalog");
    var pager = document.getElementById("plugin-catalog-pagination");
    if (!section || !container) return;
    if (!workspaceId) {
      section.hidden = true;
      return;
    }
    section.hidden = false;
    if (pager) pager.innerHTML = "";
    bindFilters();
    showMessage("", "info");

    if (MARKETPLACE && typeof MARKETPLACE.stateHtml === "function") {
      container.innerHTML = MARKETPLACE.stateHtml("loading", {
        message: "Loading marketplace\u2026",
      });
    } else {
      container.innerHTML = '<p class="empty-note">Loading marketplace\u2026</p>';
    }

    var filters = currentFilters();
    var url =
      "/plugins/api/workspaces/" +
      encodeURIComponent(workspaceId) +
      "/catalog?page=" +
      encodeURIComponent(state.page) +
      "&limit=" +
      PAGE_LIMIT +
      "&q=" +
      encodeURIComponent(filters.q) +
      "&capability=" +
      encodeURIComponent(filters.capability);

    api(url)
      .then(function (data) {
        container.innerHTML = renderCatalog(data, { canManage: state.canManage });
        if (pager) pager.innerHTML = paginationHtml(data.pagination);
        bind(workspaceId, state.page, state.canManage, state.onChanged);
      })
      .catch(function (error) {
        var payload = {
          title: "Could not load the marketplace",
          message:
            error && error.code === "timeout"
              ? "The marketplace did not respond in time. Please try again."
              : error && error.status === 404
                ? "The catalog is not configured for this deployment."
                : "We couldn\u2019t load the marketplace catalog. Please try again.",
          retry: true,
        };
        if (MARKETPLACE && typeof MARKETPLACE.errorStateHtml === "function") {
          container.innerHTML = MARKETPLACE.errorStateHtml(error, payload);
          if (typeof MARKETPLACE.bindRetry === "function") {
            MARKETPLACE.bindRetry(container, function () {
              loadCatalog(workspaceId, options);
            });
          }
        } else {
          container.innerHTML =
            '<p class="empty-note">' +
            escapeHtml(payload.message) +
            "</p>";
        }
      });
  }

  root.AICAPluginCatalog = {
    PAGE_LIMIT: PAGE_LIMIT,
    escapeHtml: escapeHtml,
    paginationHtml: paginationHtml,
    renderCatalog: renderCatalog,
    renderEntry: renderEntry,
    loadCatalog: loadCatalog,
  };
})(window);
