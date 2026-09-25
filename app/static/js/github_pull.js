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
  var filesEl = document.getElementById("pull-files");
  var analysisEl = document.getElementById("pr-analysis");

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

    var mergeState = pr.mergeable === true ? "mergeable" :
      pr.mergeable === false ? "conflict" : "unknown";
    var checks = pr.checks === null ? "Checks unavailable." :
      (pr.checks && pr.checks.length ? pr.checks.map(function (check) {
        var conclusion = check.conclusion || check.status || "pending";
        var link = check.details_url ? '<a href="' + GH.escapeHtml(check.details_url) + '" target="_blank" rel="noopener">' + GH.escapeHtml(check.name || "check") + '</a>' : GH.escapeHtml(check.name || "check");
        return '<li><span class="tag">' + GH.escapeHtml(conclusion) + '</span> ' + link + '</li>';
      }).join("") : "No check runs reported.");
    detailEl.insertAdjacentHTML("beforeend", '<section class="pr-health"><h3>Merge and CI</h3><p>Mergeable state: <strong>' + mergeState + '</strong></p><ul>' + checks + '</ul></section>');

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
