// AI Code Assistant — workspaces list page
// Creates new workspaces from the modal form.

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

  document.addEventListener("DOMContentLoaded", function () {
    var modal = document.getElementById("workspace-modal");
    var nameEl = document.getElementById("ws-name");
    var descEl = document.getElementById("ws-description");
    var createBtn = document.getElementById("create-workspace");

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

    var grid = document.getElementById("workspace-grid");

    function updatePinButton(card, pinned) {
      var button = card.querySelector('[data-action="toggle-pin"]');
      if (!button) return;
      button.classList.toggle("active", pinned);
      button.setAttribute("aria-pressed", pinned ? "true" : "false");
      button.title = pinned ? "Unpin workspace" : "Pin workspace";
      button.textContent = pinned ? "★" : "☆";
    }

    function reorderGrid() {
      if (!grid) return;
      api("/workspaces/api/workspaces")
        .then(function (workspaces) {
          workspaces.forEach(function (workspace) {
            var card = grid.querySelector('.workspace-card[data-id="' + workspace.id + '"]');
            if (!card) return;
            card.dataset.pinned = workspace.is_pinned ? "true" : "false";
            card.classList.toggle("is-pinned", workspace.is_pinned);
            updatePinButton(card, workspace.is_pinned);
            grid.appendChild(card);
          });
        })
        .catch(function (error) {
          flashError(error.message);
        });
    }

    function togglePin(card) {
      var pinned = card.dataset.pinned === "true";
      api("/workspaces/api/workspaces/" + card.dataset.id, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ is_pinned: !pinned }),
      })
        .then(reorderGrid)
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
