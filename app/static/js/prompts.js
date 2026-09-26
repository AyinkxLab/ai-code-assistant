// AI Code Assistant — prompt library UI
// Loads, filters, searches, saves, and deletes prompt templates.

(function () {
  "use strict";

  var listEl = document.getElementById("prompt-list");
  var searchEl = document.getElementById("prompt-search");
  var categoryEl = document.getElementById("prompt-category-filter");
  var favEl = document.getElementById("prompt-fav-only");
  var editorEl = document.getElementById("prompt-editor");
  var titleEl = document.getElementById("prompt-title");
  var categoryInputEl = document.getElementById("prompt-category");
  var contentEl = document.getElementById("prompt-content");
  var historyEl = document.getElementById("prompt-history");
  var versionsEl = document.getElementById("prompt-versions");
  var editingId = null;

  var CSRF_TOKEN = null;

  function getCsrf() {
    if (CSRF_TOKEN !== null) return CSRF_TOKEN;
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) {
      CSRF_TOKEN = meta.content;
      return CSRF_TOKEN;
    }
    var input = document.querySelector('input[name="csrf_token"]');
    CSRF_TOKEN = input ? input.value : "";
    return CSRF_TOKEN;
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  function api(url, options) {
    options = options || {};
    options.headers = Object.assign({}, options.headers || {}, {
      "X-CSRFToken": getCsrf(),
    });
    return fetch(url, options).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) {
          throw new Error(data && data.error ? data.error : "Request failed (" + response.status + ").");
        }
        return data;
      });
    });
  }

  function flashError(message) {
    var existing = document.querySelector(".flash-error");
    if (existing) existing.remove();
    var el = document.createElement("div");
    el.className = "flash flash-error";
    el.textContent = message;
    listEl.prepend(el);
  }

  function showImportStatus(message, isError) {
    var el = document.getElementById("prompt-import-status");
    if (!el) return;
    el.textContent = message;
    el.className = isError ? "field-hint flash-error" : "field-hint";
  }

  function importFile(file) {
    var form = new FormData();
    form.append("file", file);
    api("/prompts/api/prompts/import", { method: "POST", body: form })
      .then(function (result) {
        showImportStatus(result.message, false);
        refresh();
        loadCategories();
      })
      .catch(function (error) {
        showImportStatus(error.message, true);
      });
  }

  function refresh() {
    var params = [];
    var query = searchEl.value.trim();
    var category = categoryEl.value;
    if (query) params.push("q=" + encodeURIComponent(query));
    if (category) params.push("category=" + encodeURIComponent(category));
    if (favEl.checked) params.push("favorites=1");

    var url = "/prompts/api/prompts" + (params.length ? "?" + params.join("&") : "");
    api(url).then(render).catch(function (error) {
      flashError(error.message);
    });
  }

  function loadCategories() {
    api("/prompts/api/categories").then(function (categories) {
      var current = categoryEl.value;
      categoryEl.innerHTML = '<option value="">All categories</option>';
      categories.forEach(function (category) {
        var option = document.createElement("option");
        option.value = category;
        option.textContent = category;
        categoryEl.appendChild(option);
      });
      if (categories.indexOf(current) !== -1) categoryEl.value = current;
    }).catch(function (error) {
      flashError(error.message);
    });
  }

  function render(prompts) {
    listEl.innerHTML = "";
    if (prompts.length === 0) {
      listEl.innerHTML = '<p class="sidebar-empty">No prompts found. Create one to get started.</p>';
      return;
    }
    prompts.forEach(function (prompt) {
      var card = document.createElement("div");
      card.className = "prompt-card";
      var star = prompt.is_favorite ? "★" : "☆";
      card.innerHTML =
        '<div class="prompt-card-header">' +
        '<span class="prompt-category">' + escapeHtml(prompt.category) + "</span>" +
        '<button class="prompt-star" data-action="toggle" title="Toggle favorite">' + star + "</button>" +
        "</div>" +
        '<h3 class="prompt-title">' + escapeHtml(prompt.title) + "</h3>" +
        '<p class="prompt-preview">' + escapeHtml(prompt.content.slice(0, 140)) + "</p>" +
        '<div class="prompt-card-actions">' +
        '<button class="btn btn-ghost btn-sm" data-action="edit">Edit</button>' +
        '<button class="btn btn-ghost btn-sm btn-danger" data-action="delete">Delete</button>' +
        "</div>";
      card.dataset.id = prompt.id;
      listEl.appendChild(card);
    });
  }

  function openEditor(prompt) {
    editingId = prompt ? prompt.id : null;
    titleEl.value = prompt ? prompt.title : "";
    categoryInputEl.value = prompt ? prompt.category : "";
    contentEl.value = prompt ? prompt.content : "";
    document.getElementById("prompt-editor-title").textContent = prompt ? "Edit Prompt" : "New Prompt";
    if (prompt) {
      historyEl.hidden = false;
      versionsEl.innerHTML = '<p class="sidebar-empty">Loading versions...</p>';
      loadVersions(prompt.id);
    } else {
      historyEl.hidden = true;
      versionsEl.innerHTML = "";
    }
    editorEl.hidden = false;
    editorEl.scrollIntoView();
    titleEl.focus();
  }

  function closeEditor() {
    editorEl.hidden = true;
    historyEl.hidden = true;
    editingId = null;
  }

  function formatDate(value) {
    if (!value) return "";
    var parsed = new Date(value);
    return isNaN(parsed.getTime()) ? value : parsed.toLocaleString();
  }

  function loadVersions(id) {
    api("/prompts/api/prompts/" + id + "/versions")
      .then(renderVersions)
      .catch(function (error) {
        historyEl.hidden = true;
        flashError(error.message);
      });
  }

  function renderVersions(versions) {
    versionsEl.innerHTML = "";
    if (!versions.length) {
      versionsEl.innerHTML = '<p class="sidebar-empty">No versions yet.</p>';
      return;
    }
    var latestId = versions[versions.length - 1].id;
    versions.slice().reverse().forEach(function (version) {
      var entry = document.createElement("div");
      entry.className = "prompt-version";
      var diff = version.diff
        ? '<pre class="prompt-diff">' + escapeHtml(version.diff) + "</pre>"
        : '<p class="prompt-version-meta">Initial version.</p>';
      var action =
        version.id === latestId
          ? '<span class="prompt-version-current">current</span>'
          : '<button class="btn btn-ghost btn-sm" data-action="revert" data-version="' +
            version.id +
            '">Revert</button>';
      entry.innerHTML =
        '<div class="prompt-version-header">' +
        "<strong>v" + version.version + "</strong>" +
        '<span class="prompt-version-meta">' + escapeHtml(formatDate(version.created_at)) + "</span>" +
        action +
        "</div>" +
        '<p class="prompt-version-title">' + escapeHtml(version.title) + "</p>" +
        diff;
      versionsEl.appendChild(entry);
    });
  }

  function revertVersion(versionId) {
    if (!editingId) return;
    api("/prompts/api/prompts/" + editingId, {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ revert_to_version: versionId }),
    })
      .then(function (prompt) {
        titleEl.value = prompt.title;
        categoryInputEl.value = prompt.category;
        contentEl.value = prompt.content;
        loadVersions(prompt.id);
        refresh();
      })
      .catch(function (error) {
        flashError(error.message);
      });
  }

  function save() {
    var payload = {
      title: titleEl.value.trim(),
      content: contentEl.value.trim(),
      category: categoryInputEl.value.trim() || "General",
    };
    if (!payload.title || !payload.content) {
      flashError("Both title and content are required.");
      return;
    }
    var request = {
      method: editingId ? "PATCH" : "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    };
    api("/prompts/api/prompts" + (editingId ? "/" + editingId : ""), request)
      .then(function () {
        closeEditor();
        refresh();
        loadCategories();
      })
      .catch(function (error) {
        flashError(error.message);
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    refresh();
    loadCategories();

    searchEl.addEventListener("input", refresh);
    categoryEl.addEventListener("change", refresh);
    favEl.addEventListener("change", refresh);
    var importInput = document.getElementById("prompt-import-file");
    if (importInput) {
      importInput.addEventListener("change", function () {
        var file = importInput.files && importInput.files[0];
        if (!file) return;
        importFile(file);
        importInput.value = "";
      });
    }
    document.getElementById("new-prompt").addEventListener("click", function () {
      openEditor(null);
    });
    document.getElementById("prompt-save").addEventListener("click", save);
    document.getElementById("prompt-cancel").addEventListener("click", closeEditor);

    listEl.addEventListener("click", function (event) {
      var button = event.target.closest("button[data-action]");
      if (!button) return;
      var card = event.target.closest(".prompt-card");
      var id = card.dataset.id;
      var action = button.dataset.action;

      if (action === "edit") {
        api("/prompts/api/prompts/" + id).then(openEditor).catch(function (error) {
          flashError(error.message);
        });
      } else if (action === "delete") {
        if (!confirm("Delete this prompt?")) return;
        api("/prompts/api/prompts/" + id, { method: "DELETE" }).then(refresh).catch(function (error) {
          flashError(error.message);
        });
      } else if (action === "toggle") {
        api("/prompts/api/prompts/" + id + "/favorite", { method: "POST" }).then(refresh).catch(function (error) {
          flashError(error.message);
        });
      }
    });

    versionsEl.addEventListener("click", function (event) {
      var button = event.target.closest("button[data-action='revert']");
      if (button) revertVersion(button.dataset.version);
    });
  });
})();
