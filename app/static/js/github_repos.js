// AI Code Assistant — GitHub repository browser
// Loads and filters the connected user's repositories, and allows opening any
// public repository by owner/name.

(function () {
  "use strict";

  var GH = window.GitHub;
  var listEl = document.getElementById("repo-list");
  var searchEl = document.getElementById("repo-search");
  var lookupFormEl = document.getElementById("repo-lookup-form");
  var lookupInputEl = document.getElementById("repo-lookup-input");
  var lookupErrorEl = document.getElementById("repo-lookup-error");

  // Debounce search requests so typing does not fire one API call per keystroke.
  var SEARCH_DEBOUNCE_MS = 300;
  var searchTimer = null;

  // GitHub owner/name segments allow letters, digits, '-', '_', '.'.
  var REPO_PARTS_REGEX = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/;

  function render(repos) {
    listEl.innerHTML = "";
    if (!repos.length) {
      listEl.innerHTML = '<p class="sidebar-empty">No repositories found. Your GitHub account may need the <code>repo</code> scope.</p>';
      return;
    }
    repos.forEach(function (repo) {
      var card = document.createElement("div");
      card.className = "repo-card";
      var visibility = repo.private ? '<span class="tag tag-private">private</span>' : '<span class="tag tag-public">public</span>';
      var language = repo.language ? '<span class="repo-language">' + GH.escapeHtml(repo.language) + "</span>" : "";
      var description = repo.description
        ? '<p class="repo-description">' + GH.escapeHtml(repo.description) + "</p>"
        : "";
      var parts = repo.full_name.split("/");
      card.innerHTML =
        '<div class="repo-card-header">' +
        '<a class="repo-name" href="/github/repos/' + encodeURIComponent(parts[0]) + "/" + encodeURIComponent(parts[1]) + '">' +
        GH.escapeHtml(repo.full_name) + "</a> " + visibility +
        "</div>" +
        description +
        '<div class="repo-card-meta">' + language +
        '<span class="repo-updated">Updated ' + GH.relativeDate(repo.updated_at) + "</span>" +
        "</div>";
      listEl.appendChild(card);
    });
  }

  function refresh() {
    var query = searchEl.value.trim();
    var url = "/github/api/repos" + (query ? "?q=" + encodeURIComponent(query) : "");
    GH.api(url).then(render).catch(function (error) {
      if (error.kind === "not_connected") {
        listEl.innerHTML = '<p class="sidebar-empty">Connect your GitHub account first.</p>';
      } else {
        listEl.innerHTML = '<p class="sidebar-empty">Could not load repositories.</p>';
        GH.flashError(error.message);
      }
    });
  }

  function onSearchInput() {
    if (searchTimer) clearTimeout(searchTimer);
    searchTimer = setTimeout(refresh, SEARCH_DEBOUNCE_MS);
  }

  function showLookupError(message) {
    if (!lookupErrorEl) return;
    lookupErrorEl.textContent = message;
    lookupErrorEl.hidden = !message;
  }

  function onLookupSubmit(event) {
    event.preventDefault();
    showLookupError("");
    var value = lookupInputEl.value.trim().replace(/^\/+|\/+$/g, "");
    if (!REPO_PARTS_REGEX.test(value)) {
      showLookupError("Enter a repository as owner/name.");
      return;
    }
    var parts = value.split("/");
    window.location.href = "/github/repos/" + encodeURIComponent(parts[0]) + "/" + encodeURIComponent(parts[1]);
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!listEl || !searchEl) return;
    refresh();
    searchEl.addEventListener("input", onSearchInput);
    if (lookupFormEl) {
      lookupFormEl.addEventListener("submit", onLookupSubmit);
    }
  });
})();
