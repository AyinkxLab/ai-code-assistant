// AI Code Assistant — GitHub repository detail
// Tabs: files (tree browser), commits, issues, pull requests, AI analysis.

(function () {
  "use strict";

  var GH = window.GitHub;
  var FULL_NAME = document.querySelector(".repo-header h1").textContent.trim().split("/").slice(1).join("/").trim();
  var parts = FULL_NAME.split(" / ");
  var OWNER = parts[0];
  var REPO = parts[1];

  var state = {
    ref: "HEAD",
    filePath: "",
    issuePage: 1,
    pullPage: 1,
  };

  // -- Repo metadata -------------------------------------------------------

  function loadMeta() {
    GH.api("/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO))
      .then(function (repo) {
        var meta = document.getElementById("repo-meta");
        var description = repo.description ? GH.escapeHtml(repo.description) + " " : "";
        var visibility = repo.private ? "Private" : "Public";
        meta.innerHTML = description +
          '<span class="tag ' + (repo.private ? "tag-private" : "tag-public") + '">' + visibility + "</span> " +
          (repo.language ? '<span class="repo-language">' + GH.escapeHtml(repo.language) + "</span> " : "") +
          '<span class="repo-updated">Updated ' + GH.relativeDate(repo.updated_at) + "</span>";
        if (repo.readme) {
          meta.innerHTML += '<details class="readme"><summary>README</summary><div class="readme-body">' + GH.renderMarkdownish(repo.readme.slice(0, 3000)) + "</div></details>";
        }
      })
      .catch(function (error) {
        GH.flashError(error.message);
      });
  }

  // -- Branch selection ----------------------------------------------------

  function loadBranches() {
    GH.api("/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) + "/branches")
      .then(function (branches) {
        var select = document.getElementById("branch-select");
        if (!select) return;
        select.innerHTML = "";
        branches.forEach(function (branch) {
          var option = document.createElement("option");
          option.value = branch.name;
          option.textContent = branch.name;
          select.appendChild(option);
        });
        if (state.ref === "HEAD" && branches.length) state.ref = branches[0].name;
        select.value = state.ref;
        if (document.getElementById("tab-tree").hidden === false) loadTree();
      })
      .catch(function () { /* non-fatal */ });
  }

  // -- Files ---------------------------------------------------------------

  function loadTree() {
    var container = document.getElementById("file-browser");
    container.innerHTML = '<p class="sidebar-empty">Loading files...</p>';
    var query = document.getElementById("file-search").value.trim();
    var url = "/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) +
      "/tree?ref=" + encodeURIComponent(state.ref);
    GH.api(url).then(function (data) {
      if (data.truncated) GH.flashError("Tree truncated by GitHub (large repository).");
      var entries = data.entries || [];
      if (query) {
        entries = entries.filter(function (e) { return e.type === "blob" && e.path.toLowerCase().indexOf(query.toLowerCase()) !== -1; });
      }
      if (!entries.length) {
        container.innerHTML = '<p class="sidebar-empty">No files found.</p>';
        return;
      }
      // Show a depth-flattened tree grouped by top-level directory.
      var dirs = {};
      var files = [];
      entries.forEach(function (entry) {
        if (entry.type === "blob") {
          var idx = entry.path.indexOf("/");
          var top = idx === -1 ? "(root)" : entry.path.slice(0, idx);
          if (!dirs[top]) dirs[top] = [];
          dirs[top].push(entry);
        } else {
          files.push(entry);
        }
      });
      var html = "";
      Object.keys(dirs).sort().forEach(function (dir) {
        html += '<div class="file-dir-header">' + GH.escapeHtml(dir) + "/</div>";
        dirs[dir].slice(0, 500).forEach(function (file) {
          html += renderFileRow(file);
        });
      });
      container.innerHTML = html;
    }).catch(function (error) {
      container.innerHTML = '<p class="sidebar-empty">Could not load files.</p>';
      GH.flashError(error.message);
    });
  }

  function renderFileRow(file) {
    return '<div class="file-row" data-path="' + GH.escapeHtml(file.path) + '">' +
      "<span>" + GH.escapeHtml(file.path) + "</span>" +
      '<button class="btn btn-ghost btn-sm" data-action="open">Open</button>' +
      "</div>";
  }

  function openFile(path) {
    state.filePath = path;
    var modal = document.getElementById("file-modal");
    var title = document.getElementById("file-modal-title");
    var body = document.getElementById("file-modal-body");
    var analysis = document.getElementById("file-analysis");
    title.textContent = path;
    body.textContent = "Loading...";
    analysis.hidden = true;
    modal.hidden = false;
    GH.api("/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) +
      "/contents?path=" + encodeURIComponent(path) + "&ref=" + encodeURIComponent(state.ref))
      .then(function (data) {
        body.textContent = data.text;
      })
      .catch(function (error) {
        body.textContent = "Could not load file: " + error.message;
      });
  }

  // -- Commits -------------------------------------------------------------

  function loadCommits() {
    var container = document.getElementById("commit-list");
    container.innerHTML = '<p class="sidebar-empty">Loading commits...</p>';
    var url = "/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) +
      "/commits?ref=" + encodeURIComponent(state.ref);
    GH.api(url).then(function (commits) {
      container.innerHTML = "";
      if (!commits.length) {
        container.innerHTML = '<p class="sidebar-empty">No commits found.</p>';
        return;
      }
      commits.forEach(function (commit) {
        var row = document.createElement("div");
        row.className = "commit-row";
        row.innerHTML =
          '<span class="commit-sha">' + GH.escapeHtml(commit.short_sha) + "</span>" +
          '<span class="commit-message">' + GH.escapeHtml(commit.message) + "</span>" +
          '<span class="commit-meta">' + GH.escapeHtml(commit.author || "") + " &middot; " + GH.relativeDate(commit.date) + "</span>";
        container.appendChild(row);
      });
    }).catch(function (error) {
      container.innerHTML = '<p class="sidebar-empty">Could not load commits.</p>';
      GH.flashError(error.message);
    });
  }

  // -- Issues --------------------------------------------------------------

  function loadIssues() {
    var container = document.getElementById("issue-list");
    var pager = document.getElementById("issue-pager");
    var stateFilter = document.getElementById("issue-state").value;
    container.innerHTML = '<p class="sidebar-empty">Loading issues...</p>';
    if (pager) pager.innerHTML = "";
    var url = "/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) +
      "/issues?state=" + stateFilter + "&page=" + state.issuePage;
    GH.api(url).then(function (result) {
      var issues = result.items || [];
      container.innerHTML = "";
      if (issues.length) {
        issues.forEach(function (issue) {
          var labels = (issue.labels || []).map(function (label) {
            return '<span class="tag">' + GH.escapeHtml(label) + "</span>";
          }).join(" ");
          var row = document.createElement("div");
          row.className = "issue-row";
          row.innerHTML =
            '<a class="issue-number" href="/github/repos/' + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) + "/issues/" + issue.number + '">#' + issue.number + "</a>" +
            '<span class="issue-title">' + GH.escapeHtml(issue.title) + "</span> " + labels +
            '<span class="issue-meta">' + GH.escapeHtml(issue.author || "") + " &middot; " + GH.relativeDate(issue.created_at) + "</span>";
          container.appendChild(row);
        });
      } else {
        container.innerHTML = '<p class="sidebar-empty">No issues found.</p>';
      }
      GH.renderPager(pager, result, function (nextPage) {
        state.issuePage = nextPage;
        loadIssues();
      });
    }).catch(function (error) {
      container.innerHTML = '<p class="sidebar-empty">Could not load issues.</p>';
      GH.flashError(error.message);
    });
  }

  // -- Pull requests -------------------------------------------------------

  function loadPulls() {
    var container = document.getElementById("pull-list");
    var pager = document.getElementById("pull-pager");
    var stateFilter = document.getElementById("pull-state").value;
    container.innerHTML = '<p class="sidebar-empty">Loading pull requests...</p>';
    if (pager) pager.innerHTML = "";
    var url = "/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) +
      "/pulls?state=" + stateFilter + "&page=" + state.pullPage;
    GH.api(url).then(function (result) {
      var pulls = result.items || [];
      container.innerHTML = "";
      if (pulls.length) {
        pulls.forEach(function (pr) {
          var row = document.createElement("div");
          row.className = "issue-row";
          row.innerHTML =
            '<a class="issue-number" href="/github/repos/' + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) + "/pulls/" + pr.number + '">#' + pr.number + "</a>" +
            '<span class="issue-title">' + GH.escapeHtml(pr.title) + "</span>" +
            '<span class="issue-meta">' + GH.escapeHtml(pr.author || "") + " &middot; " + GH.relativeDate(pr.updated_at) + " &middot; " + (pr.additions || 0) + "++ / " + (pr.deletions || 0) + "--</span>";
          container.appendChild(row);
        });
      } else {
        container.innerHTML = '<p class="sidebar-empty">No pull requests found.</p>';
      }
      GH.renderPager(pager, result, function (nextPage) {
        state.pullPage = nextPage;
        loadPulls();
      });
    }).catch(function (error) {
      container.innerHTML = '<p class="sidebar-empty">Could not load pull requests.</p>';
      GH.flashError(error.message);
    });
  }

  // -- AI analysis ---------------------------------------------------------

  function analyzeRepo() {
    var container = document.getElementById("repo-analysis");
    container.hidden = false;
    container.innerHTML = '<p class="sidebar-empty">Analyzing repository...</p>';
    GH.api("/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) + "/analyze", { method: "POST" })
      .then(function (data) {
        GH.renderAnalysis(container, data.analysis);
      })
      .catch(function (error) {
        container.innerHTML = "";
        GH.flashError(error.message);
      });
  }

  function analyzeRepoStructure() {
    var container = document.getElementById("repo-analysis");
    container.hidden = false;
    container.innerHTML = '<p class="sidebar-empty">Analyzing structure and dependencies...</p>';
    GH.api("/tools/repo-analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ owner: OWNER, repo: REPO, ref: state.ref }),
    }).then(function (data) {
      GH.renderAnalysis(container, data.analysis);
    }).catch(function (error) {
      container.innerHTML = "";
      GH.flashError(error.message);
    });
  }

  function analyzeFile() {
    var question = document.getElementById("file-question").value.trim();
    var container = document.getElementById("file-analysis");
    container.hidden = false;
    container.innerHTML = '<p class="sidebar-empty">Analyzing file...</p>';
    GH.api("/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) + "/analyze-file", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ path: state.filePath, ref: state.ref, question: question }),
    }).then(function (data) {
      GH.renderAnalysis(container, data.analysis);
    }).catch(function (error) {
      container.innerHTML = "";
      GH.flashError(error.message);
    });
  }

  // -- Wiring --------------------------------------------------------------

  document.addEventListener("DOMContentLoaded", function () {
    loadMeta();
    loadBranches();

    document.querySelectorAll(".repo-tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        document.querySelectorAll(".repo-tab").forEach(function (t) { t.classList.remove("active"); });
        tab.classList.add("active");
        document.querySelectorAll(".repo-tab-panel").forEach(function (p) { p.hidden = true; });
        document.getElementById("tab-" + tab.dataset.tab).hidden = false;
        if (tab.dataset.tab === "files") loadTree();
        if (tab.dataset.tab === "commits") loadCommits();
        if (tab.dataset.tab === "issues") loadIssues();
        if (tab.dataset.tab === "pulls") loadPulls();
      });
    });

    var branchSelect = document.getElementById("branch-select");
    if (branchSelect) {
      branchSelect.addEventListener("change", function () {
        state.ref = branchSelect.value;
        if (!document.getElementById("tab-tree").hidden) loadTree();
      });
    }

    document.getElementById("file-search").addEventListener("input", function () {
      if (!document.getElementById("tab-tree").hidden) loadTree();
    });

    document.getElementById("file-browser").addEventListener("click", function (event) {
      var button = event.target.closest("button[data-action='open']");
      if (!button) return;
      var row = button.closest(".file-row");
      openFile(row.dataset.path);
    });

    document.getElementById("file-modal-close").addEventListener("click", function () {
      document.getElementById("file-modal").hidden = true;
    });
    document.getElementById("file-analyze").addEventListener("click", analyzeFile);
    document.getElementById("file-question").addEventListener("keydown", function (event) {
      if (event.key === "Enter") analyzeFile();
    });
    document.getElementById("analyze-repo").addEventListener("click", analyzeRepo);
    document.getElementById("analyze-repo-structure").addEventListener("click", analyzeRepoStructure);

    document.getElementById("issue-state").addEventListener("change", function () {
      state.issuePage = 1;
      loadIssues();
    });
    document.getElementById("pull-state").addEventListener("change", function () {
      state.pullPage = 1;
      loadPulls();
    });
  });
})();
