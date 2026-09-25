// AI Code Assistant — GitHub issue list page
(function () {
  "use strict";

  var GH = window.GitHub;
  var crumbs = document.querySelector(".github-header h1").textContent;
  var match = crumbs.match(/\/([^/]+)\/([^/]+)\s*\/\s*Issues/);
  var OWNER = match ? match[1] : "";
  var REPO = match ? match[2] : "";
  var container = document.getElementById("issue-list");
  var stateSelect = document.getElementById("issue-state");
  var pager = document.getElementById("issue-pager");
  var page = 1;

  function render(result) {
    var issues = result.items || [];
    container.innerHTML = "";
    if (!issues.length) {
      container.innerHTML = '<p class="sidebar-empty">No issues found.</p>';
    } else {
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
    }
    GH.renderPager(pager, result, function (nextPage) {
      page = nextPage;
      refresh();
    });
  }

  function refresh() {
    container.innerHTML = '<p class="sidebar-empty">Loading issues...</p>';
    if (pager) pager.innerHTML = "";
    var url = "/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) +
      "/issues?state=" + stateSelect.value + "&page=" + page;
    GH.api(url).then(render).catch(function (error) {
      container.innerHTML = '<p class="sidebar-empty">Could not load issues.</p>';
      GH.flashError(error.message);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!container || !stateSelect) return;
    refresh();
    stateSelect.addEventListener("change", function () {
      page = 1;
      refresh();
    });
  });
})();
