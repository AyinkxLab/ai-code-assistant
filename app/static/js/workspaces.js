// AI Code Assistant — workspaces list page
// Search (debounced), pagination, pin toggling, and the create modal.

(function () {
  "use strict";

  function getCsrf() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.content;
    var input = document.querySelector('input[name="csrf_token"]');
    return input ? input.value : "";
  }

  function flashError(message) {
    var el = document.createElement("div");
    el.className = "flash flash-error";
    el.textContent = message;
    var main = document.querySelector(".main-content");
    (main || document.body).prepend(el);
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

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  function formatDate(iso) {
    if (!iso) return "recently";
    var date = new Date(iso);
    if (isNaN(date.getTime())) return "recently";
    return date.toLocaleDateString(undefined, { year: "numeric", month: "short", day: "2-digit" });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var modal = document.getElementById("workspace-modal");
    var nameEl = document.getElementById("ws-name");
    var descEl = document.getElementById("ws-description");
    var createBtn = document.getElementById("create-workspace");
    var grid = document.getElementById("workspace-grid");
    var emptyEl = document.getElementById("workspace-empty");
    var pagerEl = document.getElementById("workspace-pager");
    var searchEl = document.getElementById("workspace-search");
    var searchForm = document.getElementById("workspace-search-form");

    var state = {
      q: searchEl ? searchEl.value : "",
      page: pagerEl ? parseInt(pagerEl.dataset.page, 10) || 1 : 1,
    };

    document.getElementById("new-workspace").addEventListener("click", function () {
      modal.hidden = false;
      nameEl.focus();
    });

    modal.querySelectorAll(".modal-close").forEach(function (btn) {
      btn.addEventListener("click", function () {
        modal.hidden = true;
      });
    });

    modal.addEventListener("click", function (event) {
      if (event.target === modal) modal.hidden = true;
    });

    function createWorkspace() {
      var name = nameEl.value.trim();
      if (!name) {
        nameEl.focus();
        return;
      }
      createBtn.disabled = true;
      api("/workspaces/api/workspaces", {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name, description: descEl.value.trim() }),
      })
        .then(function (workspace) {
          window.location.href = "/workspaces/" + workspace.id;
        })
        .catch(function (error) {
          flashError(error.message);
          createBtn.disabled = false;
        });
    }

    createBtn.addEventListener("click", createWorkspace);
    nameEl.addEventListener("keydown", function (event) {
      if (event.key === "Enter") createWorkspace();
    });

    // -- Search + pagination -------------------------------------------------

    function workspaceCard(workspace) {
      var count = workspace.project_count === 1 ? "1 project" : workspace.project_count + " projects";
      var pinLabel = workspace.is_pinned ? "Unpin workspace" : "Pin workspace";
      var card = document.createElement("a");
      card.className = "workspace-card" + (workspace.is_pinned ? " is-pinned" : "");
      card.href = "/workspaces/" + workspace.id;
      card.dataset.id = workspace.id;
      card.dataset.pinned = workspace.is_pinned ? "true" : "false";
      card.innerHTML =
        '<div class="workspace-card-header">' +
        '<h3 class="workspace-name">' + escapeHtml(workspace.name) + "</h3>" +
        '<span class="tag">' + escapeHtml(count) + "</span>" +
        '<button class="workspace-pin' + (workspace.is_pinned ? " active" : "") + '" type="button" data-action="toggle-pin" title="' + pinLabel + '" aria-pressed="' + (workspace.is_pinned ? "true" : "false") + '">' +
        (workspace.is_pinned ? "\u2605" : "\u2606") +
        "</button></div>" +
        (workspace.description ? '<p class="workspace-description">' + escapeHtml(workspace.description) + "</p>" : "") +
        '<p class="workspace-meta">Updated ' + escapeHtml(formatDate(workspace.updated_at)) + "</p>";
      return card;
    }

    function renderWorkspaces(items) {
      if (!grid) return;
      grid.innerHTML = "";
      items.forEach(function (workspace) {
        grid.appendChild(workspaceCard(workspace));
      });
      if (emptyEl) {
        emptyEl.hidden = items.length > 0;
        var message = emptyEl.querySelector(".sidebar-empty");
        if (message && items.length === 0) {
          message.textContent = state.q
            ? "No workspaces match your search."
            : "No workspaces yet. Create one to import a project and start exploring it.";
        }
      }
    }

    function pageLink(page, label) {
      var link = document.createElement("a");
      link.className = "btn btn-ghost btn-sm";
      link.href = "?q=" + encodeURIComponent(state.q) + "&page=" + page;
      link.dataset.page = page;
      link.textContent = label;
      return link;
    }

    function renderPager(data) {
      if (!pagerEl) return;
      pagerEl.dataset.page = data.page;
      pagerEl.dataset.total = data.total;
      pagerEl.innerHTML = "";
      var totalPages = Math.max(1, Math.ceil(data.total / data.per_page));
      if (totalPages <= 1) return;

      if (data.page > 1) pagerEl.appendChild(pageLink(data.page - 1, "Previous"));
      var info = document.createElement("span");
      info.className = "field-hint";
      info.textContent =
        "Page " + data.page + " of " + totalPages +
        " (" + data.total + " workspace" + (data.total === 1 ? "" : "s") + ")";
      pagerEl.appendChild(info);
      if (data.page < totalPages) pagerEl.appendChild(pageLink(data.page + 1, "Next"));
    }

    function refresh() {
      if (!grid) return;
      var url = "/workspaces/api/workspaces?q=" + encodeURIComponent(state.q) +
        "&page=" + state.page;
      api(url).then(function (data) {
        renderWorkspaces(data.items || []);
        renderPager(data);
      }).catch(flashError);
    }

    var searchTimer = null;
    if (searchEl) {
      searchEl.addEventListener("input", function () {
        window.clearTimeout(searchTimer);
        searchTimer = window.setTimeout(function () {
          state.q = searchEl.value.trim();
          state.page = 1;
          refresh();
        }, 300);
      });
    }
    if (searchForm) {
      searchForm.addEventListener("submit", function (event) {
        event.preventDefault();
        state.q = searchEl ? searchEl.value.trim() : "";
        state.page = 1;
        refresh();
      });
    }

    if (pagerEl) {
      pagerEl.addEventListener("click", function (event) {
        var link = event.target.closest("a[data-page]");
        if (!link) return;
        event.preventDefault();
        state.page = parseInt(link.dataset.page, 10) || 1;
        refresh();
      });
    }

    // -- Pinning -------------------------------------------------------------

    function togglePin(card) {
      var pinned = card.dataset.pinned === "true";
      api("/workspaces/api/workspaces/" + card.dataset.id, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_pinned: !pinned }),
      })
        .then(refresh)
        .catch(function (error) {
          flashError(error.message);
        });
    }

    if (grid) {
      grid.addEventListener("click", function (event) {
        var button = event.target.closest('[data-action="toggle-pin"]');
        if (!button) return;
        event.preventDefault();
        event.stopPropagation();
        var card = button.closest(".workspace-card");
        if (card) togglePin(card);
      });
    }
  });
})();
