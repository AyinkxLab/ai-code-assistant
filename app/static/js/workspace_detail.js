// AI Code Assistant — workspace detail page
// Project import (archive upload + GitHub), workspace rename/delete, and
// project deletion.

(function () {
  "use strict";

  var WORKSPACE_ID = null;

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

  function setStatus(message) {
    var status = document.getElementById("import-status");
    status.hidden = !message;
    status.textContent = message || "";
  }

  function projectUrl(project) {
    return "/workspaces/" + WORKSPACE_ID + "/projects/" + project.id;
  }

  function handleImportSuccess(project, label) {
    var stellar = project && project.stellar;
    var url = projectUrl(project);
    if (!stellar || !stellar.is_stellar) {
      flash(label + " " + project.name + " (" + project.file_count + " files).", "success");
      window.location.href = url;
      return;
    }
    flash(label + " " + project.name + " — Stellar/Soroban project detected.", "success");
    var status = document.getElementById("import-status");
    status.hidden = false;
    status.innerHTML = "";
    status.appendChild(
      document.createTextNode("Imported " + project.name + " — ")
    );
    var badge = document.createElement("span");
    badge.className = "tag tag-confirmed";
    badge.textContent = "Stellar/Soroban project";
    status.appendChild(badge);
    status.appendChild(document.createTextNode(" confidence: "));
    var code = document.createElement("code");
    code.textContent = stellar.confidence || "possible";
    status.appendChild(code);
    if (stellar.is_soroban) {
      status.appendChild(document.createTextNode(" (Soroban smart contracts)"));
    }
    status.appendChild(document.createTextNode("  "));
    var openProject = document.createElement("a");
    openProject.className = "btn btn-primary btn-sm";
    openProject.href = url;
    openProject.textContent = "Open project";
    var openStellar = document.createElement("a");
    openStellar.className = "btn btn-ghost btn-sm";
    openStellar.href = url + "?tab=stellar";
    openStellar.textContent = "Open Stellar tab";
    status.appendChild(openProject);
    status.appendChild(document.createTextNode(" "));
    status.appendChild(openStellar);
  }

  function importArchive() {
    var input = document.getElementById("import-archive");
    var btn = document.getElementById("import-archive-btn");
    if (!input.files.length) {
      flash("Choose an archive to upload.", "warning");
      return;
    }
    var data = new FormData();
    data.append("file", input.files[0]);
    btn.disabled = true;
    setStatus("Indexing archive, please wait...");
    api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/projects", {
      method: "POST",
      body: data,
    })
      .then(function (project) {
        handleImportSuccess(project, "Imported archive");
      })
      .catch(function (error) {
        setStatus("");
        flash(error.message, "error");
        btn.disabled = false;
      });
  }

  function importGithub() {
    var input = document.getElementById("import-repo");
    var btn = document.getElementById("import-github-btn");
    var repo = input.value.trim();
    if (!repo) {
      flash("Enter a repository in the form owner/name.", "warning");
      return;
    }
    btn.disabled = true;
    setStatus("Importing repository, please wait...");
    api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: "github", repo: repo }),
    })
      .then(function (project) {
        handleImportSuccess(project, "Imported repository");
      })
      .catch(function (error) {
        setStatus("");
        flash(error.message, "error");
        btn.disabled = false;
      });
  }

  function loadConnectedRepos() {
    api("/github/api/repos?per_page=100")
      .then(function (repos) {
        var datalist = document.getElementById("connected-repos");
        datalist.innerHTML = "";
        repos.forEach(function (repo) {
          var option = document.createElement("option");
          option.value = repo.full_name;
          option.textContent = repo.full_name;
          datalist.appendChild(option);
        });
      })
      .catch(function () {
        // Not connected to GitHub; users can still type owner/name manually.
      });
  }

  function formatActivityTime(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleString();
  }

  function loadActivity() {
    var list = document.getElementById("activity-list");
    if (!list) return;
    api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/activity?per_page=10")
      .then(function (data) {
        if (!data.items.length) {
          list.innerHTML = '<p class="empty-note">No activity yet.</p>';
          return;
        }
        var html = "";
        data.items.forEach(function (event) {
          var actor = event.actor_username ? event.actor_username : "system";
          html +=
            '<div class="activity-row">' +
            '<span class="activity-dot"></span>' +
            '<div class="activity-body">' +
            '<div><strong>' + actor.replace(/[<>&"]/g, "") + "</strong> " + (event.label || "").replace(/[<>&"]/g, "") + "</div>" +
            '<div class="activity-meta">' + event.event_type + "</div>" +
            "</div>" +
            '<div class="activity-time">' + formatActivityTime(event.created_at) + "</div>" +
            "</div>";
        });
        list.innerHTML = html;
      })
      .catch(function (error) {
        list.innerHTML = '<p class="empty-note">' + error.message + "</p>";
      });
  }

  document.addEventListener("DOMContentLoaded", function () {
    var wsName = document.getElementById("workspace-name");
    var wsDesc = document.getElementById("workspace-description");
    var urlParts = window.location.pathname.split("/").filter(Boolean);
    WORKSPACE_ID = parseInt(urlParts[urlParts.length - 1], 10) || 0;

    document.getElementById("import-archive-btn").addEventListener("click", importArchive);
    document.getElementById("import-github-btn").addEventListener("click", importGithub);
    loadConnectedRepos();
    loadActivity();

    document.getElementById("rename-workspace").addEventListener("click", function () {
      var name = prompt("Rename workspace:", wsName.textContent.trim());
      if (name === null) return;
      api("/workspaces/api/workspaces/" + WORKSPACE_ID, {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ name: name }),
      })
        .then(function (workspace) {
          wsName.textContent = workspace.name;
        })
        .catch(function (error) {
          flash(error.message, "error");
        });
    });

    document.getElementById("delete-workspace").addEventListener("click", function () {
      if (!confirm("Delete this workspace and all of its projects?")) return;
      api("/workspaces/api/workspaces/" + WORKSPACE_ID, { method: "DELETE" })
        .then(function () {
          window.location.href = "/workspaces/";
        })
        .catch(function (error) {
          flash(error.message, "error");
        });
    });

    document.querySelectorAll(".delete-project").forEach(function (btn) {
      btn.addEventListener("click", function () {
        var id = btn.dataset.id;
        if (!confirm("Delete this project and its indexed files?")) return;
        api("/workspaces/api/projects/" + id, { method: "DELETE" })
          .then(function () {
            btn.closest(".project-row").remove();
          })
          .catch(function (error) {
            flash(error.message, "error");
          });
      });
    });
  });
})();
