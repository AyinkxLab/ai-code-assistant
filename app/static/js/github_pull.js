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
  var checksEl = document.getElementById("pull-checks");
  var filesEl = document.getElementById("pull-files");
  var analysisEl = document.getElementById("pr-analysis");

  function mergeableLabel(state) {
    if (state === true || state === "mergeable") return "Mergeable";
    if (state === false || state === "conflicting" || state === "conflict") return "Conflict";
    return "Unknown";
  }

  function mergeableClass(state) {
    if (state === true || state === "mergeable") return "tag-public";
    if (state === false || state === "conflicting" || state === "conflict") return "tag-private";
    return "tag-unknown";
  }

  function conclusionClass(conclusion) {
    var c = (conclusion || "").toLowerCase();
    if (c === "success" || c === "neutral" || c === "skipped") return "tag-public";
    if (c === "failure" || c === "timedout" || c === "cancelled" || c === "action_required") return "tag-private";
    return "tag-unknown";
  }

  function renderChecks(pr) {
    if (!checksEl) return;
    checksEl.innerHTML = "";
    var checks = pr.check_runs;
    if (!checks || !checks.length) {
      checksEl.innerHTML = '<h3>Checks</h3><p class="sidebar-empty">No CI checks available.</p>';
      return;
    }
    var html = "<h3>Checks</h3><ul class=\"check-list\">";
    checks.forEach(function (check) {
      var conclusion = check.conclusion || check.status || "unknown";
      html += "<li class=\"check-item\">" +
        '<span class="tag ' + conclusionClass(conclusion) + '">' + GH.escapeHtml(conclusion) + "</span> " +
        GH.escapeHtml(check.name || "") +
        (check.details_url ? ' <a href="' + GH.escapeHtml(check.details_url) + '" target="_blank" rel="noopener">details</a>' : "") +
        "</li>";
    });
    html += "</ul>";
    checksEl.innerHTML = html;
  }

  function render(pr) {
    var status = pr.merged ? "merged" : pr.state;
    var mergeable = mergeableLabel(pr.mergeable);
    detailEl.innerHTML =
      '<div class="issue-detail-header">' +
      '<h2>#' + pr.number + " " + GH.escapeHtml(pr.title) + "</h2>" +
      '<div class="issue-meta">' +
      '<span class="tag ' + (status === "open" ? "tag-public" : "tag-private") + '">' + GH.escapeHtml(status) + "</span> " +
      '<span class="tag ' + mergeableClass(pr.mergeable) + '">' + GH.escapeHtml(mergeable) + "</span> " +
      GH.escapeHtml(pr.author || "") + "opened " + GH.relativeDate(pr.created_at) +
      " &middot; " + (pr.changed_files || 0) + " files, " +
      (pr.additions || 0) + "++ / " + (pr.deletions || 0) + "--" +
      "</div>" +
      "</div>" +
      '<div class="issue-body">' + GH.renderMarkdownish(pr.body) + "</div>";

    renderChecks(pr);

    filesEl.innerHTML = "";
    if (pr.files && pr.files.length) {
      filesEl.innerHTML = "<h3>Changed files</h3>";
      pr.files.forEach(function (file) {
        var details = document.createElement("details");
        details.className = "diff-file";
        var patch = file.patch ? file.patch : "(no inline diff available)";
        details.innerHTML =
          "<summary>" + GH.escapeHtml(file.filename) +
          ' <span class="diff-stats">+' + (file.additions || 0) + " / -" + (file.deletions || 0) + "</span></summary>" +
          '<pre class="code-view">' + GH.escapeHtml(patch) + "</pre>";
        filesEl.appendChild(details);
      });
    }
  }

  function load(analyze) {
    detailEl.innerHTML = '<p class="sidebar-empty">Loading pull request...</p>';
    if (checksEl) checksEl.innerHTML = "";
    var url = "/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) +
      "/pulls/" + NUMBER + (analyze ? "?analyze=1" : "");
    GH.api(url).then(function (pr) {
      render(pr);
      if (pr.analysis) {
        GH.renderAnalysis(analysisEl, pr.analysis.analysis);
      }
    }).catch(function (error) {
      detailEl.innerHTML = '<p class="sidebar-empty">Could not load pull request.</p>';
      if (checksEl) checksEl.innerHTML = "";
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
