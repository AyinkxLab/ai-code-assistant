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
          error.status = response.status;
          error.data = data || {};
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

  function pollImportStatus(projectId, label) {
    var status = document.getElementById("import-status");
    status.hidden = false;
    var attempts = 0;
    var maxAttempts = 150; // ~5 minutes at a 2s interval
    function tick() {
      api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/projects")
        .then(function (projects) {
          var project = projects.filter(function (p) { return p.id === projectId; })[0];
          if (!project) {
            status.textContent = "Import job not found.";
            return;
          }
          if (project.status === "ready") {
            finishImportSuccess(project, label);
            return;
          }
          if (project.status === "failed") {
            status.textContent = "Import failed: " + (project.error_message || "unknown error");
            flash("Import failed for " + project.name + ".", "error");
            return;
          }
          status.textContent = "Indexing " + project.name + "… " + (project.progress || 0) + "%";
          attempts += 1;
          if (attempts < maxAttempts) setTimeout(tick, 2000);
        })
        .catch(function (error) {
          status.textContent = error.message;
        });
    }
    tick();
  }

  function handleImportSuccess(project, label) {
    if (project && project.status === "indexing") {
      flash("Import started for " + project.name + ". Indexing in the background…", "success");
      pollImportStatus(project.id, label);
      return;
    }
    finishImportSuccess(project, label);
  }

  function finishImportSuccess(project, label) {
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

  function importArchive(confirmed) {
    var input = document.getElementById("import-archive");
    var btn = document.getElementById("import-archive-btn");
    if (confirmed !== true && !input.files.length) {
      flash("Choose an archive to upload.", "warning");
      return;
    }
    var data = new FormData();
    data.append("file", input.files[0]);
    if (confirmed === true) data.append("confirm", "1");
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
        btn.disabled = false;
        if (error.status === 409 && error.data && error.data.duplicate) {
          if (confirmDuplicate(error)) {
            importArchive(true);
          } else {
            flash("Duplicate import cancelled.", "info");
          }
          return;
        }
        flash(error.message, "error");
      });
  }

  // ------------------------------------------- drag & drop import (#91)

  function readFileAsText(file) {
    return new Promise(function (resolve, reject) {
      var reader = new FileReader();
      reader.onload = function () {
        resolve(reader.result == null ? "" : String(reader.result));
      };
      reader.onerror = function () {
        reject(reader.error || new Error("Could not read " + file.name));
      };
      reader.readAsText(file);
    });
  }

  function entryFile(entry) {
    return new Promise(function (resolve, reject) {
      entry.file(resolve, reject);
    });
  }

  function readAllDirEntries(reader) {
    return new Promise(function (resolve, reject) {
      var all = [];
      function readBatch() {
        reader.readEntries(function (batch) {
          if (!batch.length) {
            resolve(all);
            return;
          }
          all = all.concat(batch);
          readBatch();
        }, reject);
      }
      readBatch();
    });
  }

  function walkEntry(entry, prefix, files) {
    if (!entry) return Promise.resolve();
    if (entry.isFile) {
      return entryFile(entry).then(function (file) {
        return readFileAsText(file).then(function (content) {
          files.push({ path: prefix + entry.name, content: content });
        });
      });
    }
    if (entry.isDirectory) {
      return readAllDirEntries(entry.createReader()).then(function (children) {
        return children.reduce(function (chain, child) {
          return chain.then(function () {
            return walkEntry(child, prefix + entry.name + "/", files);
          });
        }, Promise.resolve());
      });
    }
    return Promise.resolve();
  }

  function collectDroppedFiles(dataTransfer) {
    var files = [];
    var promises = [];
    var items = dataTransfer.items;
    var i;
    if (items && items.length && items[0].webkitGetAsEntry) {
      for (i = 0; i < items.length; i++) {
        var entry = items[i].webkitGetAsEntry();
        if (entry) promises.push(walkEntry(entry, "", files));
      }
    } else {
      for (i = 0; i < dataTransfer.files.length; i++) {
        (function (file) {
          promises.push(
            readFileAsText(file).then(function (content) {
              files.push({ path: file.name, content: content });
            })
          );
        })(dataTransfer.files[i]);
      }
    }
    return Promise.all(promises).then(function () {
      return files;
    });
  }

  function uploadManifest(files) {
    var name = "";
    if (files.length === 1) {
      name = files[0].path.split("/").pop().replace(/\.[^.]+$/, "");
    }
    setStatus("Importing " + files.length + " file(s), please wait...");
    return api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: "manifest", name: name, files: files }),
    })
      .then(function (project) {
        handleImportSuccess(project, "Imported files");
      })
      .catch(function (error) {
        setStatus("");
        flash(error.message, "error");
      });
  }

  function handleDrop(event) {
    event.preventDefault();
    var dropzone = document.getElementById("import-dropzone");
    if (dropzone) dropzone.classList.remove("import-dropzone-active");
    collectDroppedFiles(event.dataTransfer)
      .then(function (files) {
        if (!files.length) {
          flash("No importable files were found in the drop.", "warning");
          return;
        }
        return uploadManifest(files);
      })
      .catch(function (error) {
        flash(error.message, "error");
      });
  }

  function wireDropzone() {
    var dropzone = document.getElementById("import-dropzone");
    if (!dropzone) return;
    ["dragenter", "dragover"].forEach(function (name) {
      dropzone.addEventListener(name, function (event) {
        event.preventDefault();
        dropzone.classList.add("import-dropzone-active");
      });
    });
    ["dragleave", "dragend"].forEach(function (name) {
      dropzone.addEventListener(name, function () {
        dropzone.classList.remove("import-dropzone-active");
      });
    });
    dropzone.addEventListener("drop", handleDrop);

    var picker = document.getElementById("import-dropzone-input");
    var browse = document.getElementById("import-dropzone-browse");
    if (browse && picker) browse.addEventListener("click", function () { picker.click(); });
    if (picker) {
      picker.addEventListener("change", function () {
        var picked = Array.prototype.slice.call(picker.files || []);
        picker.value = "";
        if (!picked.length) return;
        Promise.all(
          picked.map(function (file) {
            return readFileAsText(file).then(function (content) {
              return { path: file.name, content: content };
            });
          })
        )
          .then(uploadManifest)
          .catch(function (error) {
            flash(error.message, "error");
          });
      });
    }
  }

  function importGithub(confirmed) {
    var input = document.getElementById("import-repo");
    var btn = document.getElementById("import-github-btn");
    var repo = input.value.trim();
    if (!repo) {
      flash("Enter a repository in the form owner/name.", "warning");
      return;
    }
    var payload = { source: "github", repo: repo };
    if (confirmed === true) payload.confirm = true;
    btn.disabled = true;
    setStatus("Importing repository, please wait...");
    api("/workspaces/api/workspaces/" + WORKSPACE_ID + "/projects", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    })
      .then(function (project) {
        handleImportSuccess(project, "Imported repository");
      })
      .catch(function (error) {
        setStatus("");
        btn.disabled = false;
        if (error.status === 409 && error.data && error.data.duplicate) {
          if (confirmDuplicate(error)) {
            importGithub(true);
          } else {
            flash("Duplicate import cancelled.", "info");
          }
          return;
        }
        flash(error.message, "error");
      });
  }

  function confirmDuplicate(error) {
    var existing = (error.data && error.data.duplicate_of) || {};
    var name = existing.name || "an existing project";
    var when = existing.created_at ? new Date(existing.created_at).toLocaleString() : "";
    var detail = when ? " (imported " + when + ")" : "";
    return window.confirm(
      "A matching project already exists in this workspace: " + name + detail + ".\n\n" +
        "Import another copy anyway?"
    );
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

  function metricCard(value, label) {
    return (
      '<div class="metric-card"><span class="metric-value">' + value +
      '</span><span class="metric-label">' + label + "</span></div>"
    );
  }

  function trendSection(trend) {
    if (!trend || !trend.length) return "";
    var max = 0;
    trend.forEach(function (point) { max = Math.max(max, point.total || 0); });
    max = max || 1;
    var html = "<h3 class='metric-title'>Findings addressed over time</h3><ul class='trend-list'>";
    trend.forEach(function (point) {
      var openPct = Math.round(((point.open || 0) / max) * 100);
      var donePct = Math.round(((point.addressed || 0) / max) * 100);
      html +=
        "<li class='trend-row'>" +
        "<span class='trend-date'>" + formatActivityTime(point.created_at) + "</span>" +
        "<span class='trend-bar'>" +
        "<span class='trend-open' style='width:" + openPct + "%'></span>" +
        "<span class='trend-done' style='width:" + donePct + "%'></span>" +
        "</span>" +
        "<span class='trend-counts'>" + (point.open || 0) + " open &middot; " +
        (point.addressed || 0) + " addressed</span>" +
        "</li>";
    });
    return html + "</ul>";
  }

  function loadWorkspaceMetrics() {
    var el = document.getElementById("workspace-metrics");
    if (!el) return;
    api("/reviews/api/metrics?workspace_id=" + WORKSPACE_ID)
      .then(function (metrics) {
        var findings = metrics.findings || {};
        el.innerHTML =
          '<h2>Quality</h2><div class="metric-grid">' +
          metricCard(metrics.total_reviews || 0, "reviews") +
          metricCard(findings.total || 0, "findings") +
          metricCard(findings.high_risk || 0, "high risk") +
          metricCard(findings.unaddressed_high_risk || 0, "open high risk") +
          metricCard(findings.addressed || 0, "addressed") +
          metricCard(metrics.reviews_last_7_days || 0, "reviews / 7d") +
          "</div>" +
          trendSection(metrics.findings_trend);
      })
      .catch(function () {
        el.innerHTML = '<p class="empty-note">Quality metrics unavailable.</p>';
      });
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

    document.getElementById("import-archive-btn").addEventListener("click", function () {
      importArchive(false);
    });
    document.getElementById("import-github-btn").addEventListener("click", function () {
      importGithub(false);
    });
    wireDropzone();
    loadConnectedRepos();
    loadActivity();
    loadWorkspaceMetrics();

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
