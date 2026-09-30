// AI Code Assistant — GitHub repository browser
// Loads and filters the connected user's repositories, and allows looking up any public repository by owner/name.

(function () {
  "use strict";

  var GH = window.GitHub;
  var listEl = document.getElementById("repo-list");
  var searchEl = document.getElementById("repo-search");
  var lookupForm = document.getElementById("repo-lookup-form");
  var lookupInput = document.getElementById("repo-lookup-input");

  // Debounce search requests so typing does not fire one API call per keystroke.
  var SEARCH_DEBOUNCE_MS = 300;
  var searchTimer = null;

  // Matches `owner/name` with GitHub's own allowed characters.
  var REPO_PATTN = /^[A-Za-z0-9._-]+\/[A-Za-z0-9._-]+$/;

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

  // Look up a specific repository by owner/name. The server proxies this to
  // GitHub with the user's token, so GitHub's own access model is respected:
  // public repos are readable, private repos the user cannot see return a
  // clear permission error.
  function lookupRepository(fullName) {
    listEl.innerHTML = '<p class="sidebar-empty">Loading repository…</p>';
    GH.api("/github/api/repos/" + encodeURIComponent(fullName))
      .then(function (repo) {
        render([repo]);
      })
      .catch(function (error) {
        if (error.kind === "not_connected") {
          listEl.innerHTML = '<p class="sidebar-empty">Connect your GitHub account first.</p>';
        } else if (error.kind === "not_found") {
          listEl.innerHTML = '<p class="sidebar-empty">Repository not found.</p>';
        } else if (error.kind === "forbidden" || error.status === 403) {
          listEl.innerHTML = '<p class="sidebar-empty">You do not have permission to view this repository.</p>';
        } else {
          listEl.innerHTML = '<p class="sidebar-empty">Could not load repository.</p>';
        }
        GH.flashError(error.message);
      });
  }

  function onLookupSubmit(event) {
    event.preventDefault();
    var value = (lookupInput.value || "").trim();
    if (!value) return;
    if (!REPO_PATTN.test(value)) {
      listEl.innerHTML = '<p class="sidebar-empty">Enter a repository as owner/name.</p>';
      GH.flashError("Enter a repository as owner/name.");
      return;
    }
    lookupRepository(value);
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!listEl || !searchEl) return;
    refresh();
    searchEl.addEventListener("input", onSearchInput);
    if (lookupForm) {
      lookupForm.addEventListener("submit", onLookupSubmit);
    }
  });
})();
