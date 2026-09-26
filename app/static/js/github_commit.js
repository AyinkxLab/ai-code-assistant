// AI Code Assistant — GitHub commit detail page with per-file diffs.
// Fetches GET /github/api/repos/<owner>/<repo>/commits/<sha> and renders the
// full commit message, author/date, and each changed file's patch with
// added/removed color coding.

(function () {
  "use strict";

  var GH = window.GitHub;
  var root = document.getElementById("commit-detail");
  if (!root) return;

  var OWNER = root.dataset.owner;
  var REPO = root.dataset.repo;
  var SHA = root.dataset.sha;

  function fileBlock(file) {
    var additions = file.additions || 0;
    var deletions = file.deletions || 0;
    var status = file.status
      ? '<span class="tag">' + GH.escapeHtml(file.status) + "</span> "
      : "";
    return (
      '<details class="diff-file" open>' +
      "<summary>" +
      status +
      GH.escapeHtml(file.filename || "") +
      ' <span class="diff-stats">+' + additions + " / -" + deletions + "</span></summary>" +
      GH.renderPatch(file.patch) +
      "</details>"
    );
  }

  function render(commit) {
    var files = commit.files || [];
    var filesHtml = files.length
      ? files.map(fileBlock).join("")
      : '<p class="sidebar-empty">This commit has no file changes.</p>';
    root.innerHTML =
      '<div class="commit-detail-header">' +
      '<div class="commit-detail-message">' + GH.renderMarkdownish(commit.message || "") + "</div>" +
      '<div class="commit-meta">' +
      '<span class="commit-sha">' + GH.escapeHtml(commit.short_sha || "") + "</span> " +
      GH.escapeHtml(commit.author || "unknown") +
      " committed " + GH.relativeDate(commit.date) +
      (commit.html_url
        ? ' &middot; <a href="' + GH.escapeHtml(commit.html_url) +
          '" target="_blank" rel="noopener">View on GitHub</a>'
        : "") +
      "</div>" +
      '<div class="diff-stats">' + files.length + " file" +
      (files.length === 1 ? "" : "s") + " changed</div>" +
      "</div>" +
      '<div class="commit-files">' + filesHtml + "</div>";
  }

  GH.api(
    "/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) +
      "/commits/" + encodeURIComponent(SHA)
  )
    .then(render)
    .catch(function (error) {
      root.innerHTML = '<p class="sidebar-empty">Could not load commit.</p>';
      GH.flashError(error.message);
    });
})();
