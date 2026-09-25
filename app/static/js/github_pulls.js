// AI Code Assistant — GitHub pull request list page
(function () {
  "use strict";

  var GH = window.GitHub;
  var crumbs = document.querySelector(".github-header h1").textContent;
  var match = crumbs.match(/\/([^/]+)\/([^/]+)\s*\/\s*Pull\s+Requests/);
  var OWNER = match ? match[1] : "";
  var REPO = match ? match[2] : "";
  var container = document.getElementById("pull-list");
  var stateSelect = document.getElementById("pull-state");
  var pager = document.getElementById("pull-pager");
  var page = 1;

  function render(result) {
    var pulls = result.items || [];
    container.innerHTML = "";
    if (!pulls.length) {
      container.innerHTML = '<p class="sidebar-empty">No pull requests found.</p>';
    } else {
      pulls.forEach(function (pr) {
        var row = document.createElement("div");
        row.className = "issue-row";
        row.innerHTML =
          '<a class="issue-number" href="/github/repos/' + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) + "/pulls/" + pr.number + '">#' + pr.number + "</a>" +
          '<span class="issue-title">' + GH.escapeHtml(pr.title) + "</span>" +
          '<span class="issue-meta">' + GH.escapeHtml(pr.author || "") + " &middot; " + GH.relativeDate(pr.updated_at) +
          " &middot; " + (pr.additions || 0) + "++ / " + (pr.deletions || 0) + "--</span>";
        container.appendChild(row);
      });
    }
    GH.renderPager(pager, result, function (nextPage) {
      page = nextPage;
      refresh();
    });
  }

  function refresh() {
    container.innerHTML = '<p class="sidebar-empty">Loading pull requests...</p>';
    if (pager) pager.innerHTML = "";
    var url = "/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) +
      "/pulls?state=" + stateSelect.value + "&page=" + page;
    GH.api(url).then(render).catch(function (error) {
      container.innerHTML = '<p class="sidebar-empty">Could not load pull requests.</p>';
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
