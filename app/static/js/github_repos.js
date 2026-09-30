// AI Code Assistant — GitHub repository browser
// Loads and filters the connected user's repositories and allows opening any public repo by owner/name.

(function () {
  "use strict";

  var GH = window.GitHub;
  var listEl = document.getElementById("repo-list");
  var searchEl = document.getElementById("repo-search");
  var lookupForm = document.getElementById("repo-lookup-form");
  var lookupInput = document.getElementById("repo-lookup-input");
  var lookupError = document.getElementById("repo-lookup-error");

  // Debounce search requests so typing does not fire one API call per keystroke.
  var SEARCH_DEBOUNCE_MS = 300;
  var searchTimer = null;

  // GitHub allows owner names with alphanumeric characters and hyphens, and
  // repo names with alphanumeric, hyphens, underscores, and periods.
  var OWNER_PATTERN = /^[A-Za-z0-9-]+$/;
  var REPO_PATTERN = /^[A-Za-z0-9_.-]+$/;

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
    if (!lookupError) return;
    lookupError.textContent = message;
    lookupError.hidden = false;
  }

  function clearLookupError() {
    if (!lookupError) return;
    lookupError.textContent = "";
    lookupError.hidden = true;
  }

  function parseRepoReference(value) {
    var trimmed = (value || "").trim();
    if (!trimmed) return null;
    // Accept full URLs and normalize to owner/name.
    trimmed = trimmed.replace(/^https?:\/\/github.com\//i, "");
    trimmed = trimmed.replace(/^git@github.com:/i, "");
    trimmed = trimmed.replace(/\/+$/, "").replace(/\.git$/i, "");
    var parts = trimmed.split("/");
    if (parts.length !== 2) return null;
    var owner = parts[0];
    var name = parts[1];
    if (!OWNER_PATTERN.test(owner) || !REPO_PATTERN.test(name)) return null;
    return { owner: owner, name: name };
  }

  function onLookupSubmit(event) {
    event.preventDefault();
    clearLookupError();
    var ref = parseRepoReference(lookupInput ? lookupInput.value : "");
    if (!ref) {
      showLookupError("Enter a repository as owner/name.");
      return;
    }
    var url = "/github/api/repos/" + encodeURIComponent(ref.owner) + "/" + encodeURIComponent(ref.name);
    GH.api(url).then(function () {
      window.location.href = "/github/repos/" + encodeURIComponent(ref.owner) + "/" + encodeURIComponent(ref.name);
    }).catch(function (error) {
      if (error.kind === "not_connected") {
        showLookupError("Connect your GitHub account first.");
      } else if (error.status === 403 || error.status === 404) {
        showLookupError("Repository not found or you do not have permission to access it.");
      } else {
        showLookupError(error.message || "Could not open repository.");
      }
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (listEl && searchEl) {
      refresh();
      searchEl.addEventListener("input", onSearchInput);
    }
    if (lookupForm) {
      lookupForm.addEventListener("submit", onLookupSubmit);
    }
  });
})();
