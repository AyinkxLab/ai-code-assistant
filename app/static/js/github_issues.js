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
  var labelInput = document.getElementById("issue-label");
  var assigneeInput = document.getElementById("issue-assignee");
  var searchInput = document.getElementById("issue-search");
  var filtersEl = document.getElementById("issue-filters");
  var pager = document.getElementById("issue-pager");
  var page = 1;

  function activeFilters() {
    var filters = [];
    if (labelInput && labelInput.value.trim()) {
      filters.push({ key: "label", label: "Label: " + labelInput.value.trim(), value: labelInput.value.trim() });
    }
    if (assigneeInput && assigneeInput.value.trim()) {
      filters.push({ key: "assignee", label: "Assignee: " + assigneeInput.value.trim(), value: assigneeInput.value.trim() });
    }
    if (searchInput && searchInput.value.trim()) {
      filters.push({ key: "search", label: "Search: " + searchInput.value.trim(), value: searchInput.value.trim() });
    }
    return filters;
  }

  function renderFilters() {
    if (!filtersEl) return;
    var filters = activeFilters();
    filtersEl.innerHTML = "";
    if (!filters.length) return;
    filters.forEach(function (filter) {
      var chip = document.createElement("button");
      chip.type = "button";
      chip.className = "filter-chip";
      chip.setAttribute("data-filter", filter.key);
      chip.innerHTML = GH.escapeHtml(filter.label) + ' <span aria-hidden="true">&times;</span>';
      chip.addEventListener("click", function () {
        clearFilter(filter.key);
      });
      filtersEl.appendChild(chip);
    });
  }

  function clearFilter(key) {
    if (key === "label" && labelInput) labelInput.value = "";
    if (key === "assignee" && assigneeInput) assigneeInput.value = "";
    if (key === "search" && searchInput) searchInput.value = "";
    page = 1;
    refresh();
  }

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

  function buildQuery() {
    var params = ["state=" + encodeURIComponent(stateSelect.value), "page=" + page];
    if (labelInput && labelInput.value.trim()) {
      params.push("label=" + encodeURIComponent(labelInput.value.trim()));
    }
    if (assigneeInput && assigneeInput.value.trim()) {
      params.push("assignee=" + encodeURIComponent(assigneeInput.value.trim()));
    }
    if (searchInput && searchInput.value.trim()) {
      params.push("search=" + encodeURIComponent(searchInput.value.trim()));
    }
    return params.join("&");
  }

  function refresh() {
    container.innerHTML = '<p class="sidebar-empty">Loading issues...</p>';
    if (pager) pager.innerHTML = "";
    renderFilters();
    var url = "/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) +
      "/issues?" + buildQuery();
    GH.api(url).then(render).catch(function (error) {
      container.innerHTML = '<p class="sidebar-empty">Could not load issues.</p>';
      GH.flashError(error.message);
    });
  }

  function onFilterChange() {
    page = 1;
    refresh();
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!container || !stateSelect) return;
    refresh();
    stateSelect.addEventListener("change", onFilterChange);
    [labelInput, assigneeInput, searchInput].forEach(function (input) {
      if (!input) return;
      input.addEventListener("change", onFilterChange);
      input.addEventListener("keydown", function (event) {
        if (event.key === "Enter") onFilterChange();
      });
    });
  });
})();
