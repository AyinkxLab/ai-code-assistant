// AI Code Assistant — GitHub pull request detail page with AI review
(function () {
  "use strict";

  var GH = window.GitHub;
  var crumbs = document.querySelector(".github-header h1").textContent;
  var match = crumbs.match(/\/([^/]+)\/([^/]+)\s*\/\s*Pull\s+Requests\s*\/\s*#\d+/);
  var OWNER = match ? match[1] : "";
  var REPO = match ? match[2] : "";
  var NUMBER = null;
  var numberMatch = crumbs.match(/#(\d+)/);
  if (numberMatch) NUMBER = numberMatch[1];

  var detailEl = document.getElementById("pull-detail");
  var statusEl = document.getElementById("pull-status");
  var filesEl = document.getElementById("pull-files");
  var analysisEl = document.getElementById("pr-analysis");

  function mergeableLabel(value) {
    if (value === true) return { text: "mergeable", cls: "tag-public" };
    if (value === false) return { text: "conflict", cls: "tag-private" };
    return { text: "unknown", cls: "tag-private" };
  }

  function checkConclusionClass(conclusion) {
    if (conclusion === "success") return "tag-public";
    return "tag-private";
  }

  function renderStatus(pr) {
    if (!statusEl) return;
    var merge = mergeableLabel(pr.mergeable);
    var html = '<h3>Merge status</h3>';
    html += '<div class="issue-meta"><span class="tag ' + merge.cls + '">' + GH.escapeHtml(merge.text) + '</span></div>';

    html += '<h3>CI checks</h3>';
    var checks = pr.check_runs;
    if (!checks || !checks.length) {
      html += '<p class="sidebar-empty">No CI checks available.</p>';
    } else {
      html += '<ul class="check-list">';
      checks.forEach(function (check) {
        var conclusion = check.conclusion || check.status || "unknown";
        html += '<li><span class="tag ' + checkConclusionClass(conclusion) + '">' +
          GH.escapeHtml(conclusion) + '</span> ' + GH.escapeHtml(check.name || "") + '</li>';
      });
      html += '</ul>';
    }
    statusEl.innerHTML = html;
  }

  function render(pr) {
    var status = pr.merged ? "merged" : pr.state;
    detailEl.innerHTML =
      '<div class="issue-detail-header">' +
      '<h2>#' + pr.number + " " + GH.escapeHtml(pr.title) + "</h2>" +
      '<div class="issue-meta">' +
      '<span class="tag ' + (status === "open" ? "tag-public" : "tag-private") + '">' + GH.escapeHtml(status) + "</span> " +
      GH.escapeHtml(pr.author || "") + " opened " + GH.relativeDate(pr.created_at) +
      " &middot; " + (pr.changed_files || 0) + " files, " +
      (pr.additions || 0) + "++ / " + (pr.deletions || 0) + "--" +
      "</div>" +
      "</div>" +
      '<div class="issue-body">' + GH.renderMarkdownish(pr.body) + "</div>";

    renderStatus(pr);

    filesEl.innerHTML = "";
    if (pr.files && pr.files.length) {
      filesEl.innerHTML = "<h3>Changed files</h3>";
      pr.files.forEach(function (file) {
        var details = document.createElement("details");
        details.className = "diff-file";
        var patch = file.patch ? GH.escapeHtml(file.patch) : "(no inline diff available)";
        details.innerHTML =
          "<summary>" + GH.escapeHtml(file.filename) +
          ' <span class="diff-stats">+' + (file.additions || 0) + " / -" + (file.deletions || 0) + "</span></summary>" +
          '<pre class="code-view">' + patch + "</pre>";
        filesEl.appendChild(details);
      });
    }
  }

  function load(analyze) {
    detailEl.innerHTML = '<p class="sidebar-empty">Loading pull request...</p>';
    var url = "/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) +
      "/pulls/" + NUMBER + (analyze ? "?analyze=1" : "");
    GH.api(url).then(function (pr) {
      render(pr);
      if (pr.analysis) {
        GH.renderAnalysis(analysisEl, pr.analysis.analysis);
      }
    }).catch(function (error) {
      detailEl.innerHTML = '<p class="sidebar-empty">Could not load pull request.</p>';
      GH.flashError(error.message);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!detailEl) return;
    load(false);
    document.getElementById("analyze-pr").addEventListener("click", function () {
      analysisEl.hidden = false;
      analysisEl.innerHTML = '<p class="sidebar-empty">Analyzing pull request...</p>';
      GH.api("/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) +
        "/pulls/" + NUMBER + "?analyze=1")
        .then(function (pr) {
          GH.renderAnalysis(analysisEl, pr.analysis.analysis);
        })
        .catch(function (error) {
          analysisEl.innerHTML = "";
          GH.flashError(error.message);
        });
    });
  });
})();
