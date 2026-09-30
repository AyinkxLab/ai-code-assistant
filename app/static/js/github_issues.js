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

  function setupNewIssueForm() {
    var form = document.getElementById("new-issue-form");
    if (!form) return;
    var titleInput = document.getElementById("new-issue-title");
    var bodyInput = document.getElementById("new-issue-body");
    var submitBtn = document.getElementById("new-issue-submit");
    var notice = document.getElementById("new-issue-notice");
    var canTrigger = document.getElementById("new-issue-toggle");

    function applyPermissions(perms) {
      var allowed = !!(perms && perms.can_write_issues);
      [titleInput, bodyInput, submitBtn].forEach(function (el) {
        if (!el) return;
        el.disabled = !allowed;
      });
      if (notice) {
        notice.hidden = allowed;
        notice.textContent = allowed
          ? ""
          : "Your GitHub token lacks the scope required to open issues.";
      }
    }

    GH.api("/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) + "/permissions")
      .then(function (perms) {
        applyPermissions(perms);
      })
      .catch(function () {
        applyPermissions({can_write_issues: false});
      });

    if (canTrigger) {
      canTrigger.addEventListener("click", function () {
        form.hidden = !form.hidden;
      });
    }

    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var title = (titleInput.value || "").trim();
      if (!title) {
        GH.flashError("An issue title is required.");
        return;
      }
      submitBtn.disabled = true;
      GH.api("/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) + "/issues", {
        method: "POST",
        body: JSON.stringify({title: title, body: bodyInput ? bodyInput.value : ""}),
      }).then(function (issue) {
        titleInput.value = "";
        if (bodyInput) bodyInput.value = "";
        form.hidden = true;
        window.location.href = "/github/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) + "/issues/" + issue.number;
      }).catch(function (error) {
        GH.flashError(error.message);
      }).finally(function () {
        submitBtn.disabled = false;
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!container || !stateSelect) return;
    refresh();
    stateSelect.addEventListener("change", function () {
      page = 1;
      refresh();
    });
    setupNewIssueForm();
  });
})();
