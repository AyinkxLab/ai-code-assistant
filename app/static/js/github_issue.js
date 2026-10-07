// AI Code Assistant — GitHub issue detail page with AI analysis
(function () {
  "use strict";

  var GH = window.GitHub;
  var crumbs = document.querySelector(".github-header h1").textContent;
  var match = crumbs.match(/\/([^/]+)\/([^/]+)\s*\/\s*Issues\s*\/\s*#\d+/);
  var OWNER = match ? match[1] : "";
  var REPO = match ? match[2] : "";
  var NUMBER = null;
  var numberMatch = crumbs.match(/#(\d+)/);
  if (numberMatch) NUMBER = numberMatch[1];

  var detailEl = document.getElementById("issue-detail");
  var analysisEl = document.getElementById("issue-analysis");
  var commentsEl = document.getElementById("issue-comments");
  var commentList = document.getElementById("issue-comment-list");
  var commentForm = document.getElementById("issue-comment-form");
  var commentBody = document.getElementById("issue-comment-body");
  var commentSubmit = document.getElementById("issue-comment-submit");
  var commentNotice = document.getElementById("issue-comment-notice");
  var canComment = false;

  function render(issue) {
    var labels = (issue.labels || []).map(function (label) {
      return '<span class="tag">' + GH.escapeHtml(label) + "</span>";
    }).join(" ");
    detailEl.innerHTML =
      '<div class="issue-detail-header">' +
      '<h2>#' + issue.number + " " + GH.escapeHtml(issue.title) + "</h2>" +
      '<div class="issue-meta">' + labels + " " +
      '<span class="tag ' + (issue.state === "open" ? "tag-public" : "tag-private") + '">' + GH.escapeHtml(issue.state) + "</span> " +
      GH.escapeHtml(issue.author || "") + "opened " + GH.relativeDate(issue.created_at) +
      (issue.comments ? " &middot; " + issue.comments + " comments" : "") +
      "</div>" +
      "</div>" +
      '<div class="issue-body">' + GH.renderMarkdownish(issue.body) + "</div>";
  }

  function renderComments(comments) {
    if (!commentList) return;
    commentList.innerHTML = "";
    if (!comments || !comments.length) {
      commentList.innerHTML = '<p class="sidebar-empty">No comments yet.</p>';
      return;
    }
    comments.forEach(function (comment) {
      var row = document.createElement("div");
      row.className = "issue-comment";
      row.innerHTML =
        '<div class="issue-comment-meta">' + GH.escapeHtml(comment.author || "") + " &middot; " + GH.relativeDate(comment.created_at) + "</div>" +
        '<div class="issue-comment-body">' + GH.renderMarkdownish(comment.body) + "</div>";
      commentList.appendChild(row);
    });
  }

  function loadComments() {
    if (!commentList) return;
    commentList.innerHTML = '<p class="sidebar-empty">Loading comments...</p>';
    GH.api("/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) +
      "/issues/" + NUMBER + "/comments")
      .then(function (result) {
        renderComments(result.items || []);
      })
      .catch(function (error) {
        commentList.innerHTML = '<p class="sidebar-empty">Could not load comments.</p>';
        GH.flashError(error.message);
      });
  }

  function applyCommentPermissions(perms) {
    canComment = !!(perms && perms.can_comment);
    if (commentBody) commentBody.disabled = !canComment;
    if (commentSubmit) commentSubmit.disabled = !canComment;
    if (commentNotice) {
      commentNotice.hidden = canComment;
      commentNotice.textContent = canComment
        ? ""
        : "Your GitHub token lacks the scope required to comment.";
    }
  }

  function loadPermissions() {
    GH.api("/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) + "/permissions")
      .then(function (perms) {
        applyCommentPermissions(perms);
      })
      .catch(function () {
        applyCommentPermissions({can_comment: false});
      });
  }

  function load(analyze) {
    detailEl.innerHTML = '<p class="sidebar-empty">Loading issue...</p>';
    var url = "/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) +
      "/issues/" + NUMBER + (analyze ? "?analyze=1" : "");
    GH.api(url).then(function (issue) {
      render(issue);
      if (issue.analysis) {
        GH.renderAnalysis(analysisEl, issue.analysis.analysis);
      }
    }).catch(function (error) {
      detailEl.innerHTML = '<p class="sidebar-empty">Could not load issue.</p>';
      GH.flashError(error.message);
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (!detailEl) return;
    load(false);
    loadComments();
    loadPermissions();
    document.getElementById("analyze-issue").addEventListener("click", function () {
      analysisEl.hidden = false;
      analysisEl.innerHTML = '<p class="sidebar-empty">Analyzing issue...</p>';
      GH.api("/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) +
        "/issues/" + NUMBER + "?analyze=1")
        .then(function (issue) {
          GH.renderAnalysis(analysisEl, issue.analysis.analysis);
        })
        .catch(function (error) {
          analysisEl.innerHTML = "";
          GH.flashError(error.message);
        });
    });

    if (commentForm) {
      commentForm.addEventListener("submit", function (event) {
        event.preventDefault();
        if (!canComment) return;
        var body = (commentBody.value || "").trim();
        if (!body) {
          GH.flashError("A comment body is required.");
          return;
        }
        commentSubmit.disabled = true;
        GH.api("/github/api/repos/" + encodeURIComponent(OWNER) + "/" + encodeURIComponent(REPO) +
          "/issues/" + NUMBER + "/comments", {
          method: "POST",
          body: JSON.stringify({body: body}),
        }).then(function () {
          commentBody.value = "";
          loadComments();
        }).catch(function (error) {
          GH.flashError(error.message);
        }).finally(function () {
          commentSubmit.disabled = !canComment;
        });
      });
    }
  });
})();
