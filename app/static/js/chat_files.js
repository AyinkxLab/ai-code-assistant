// AI Code Assistant — multi-file project tree in the chat sidebar.
// Renders the user's workspaces → projects → files, opens a file with a
// lightweight syntax highlighter, and offers an "Ask about this file" action.

(function () {
  "use strict";

  var treeEl = document.getElementById("project-file-tree");
  if (!treeEl) return;
  var viewerEl = document.getElementById("project-file-viewer");
  var metaEl = document.getElementById("project-file-meta");
  var contentEl = document.getElementById("project-file-content");
  var backBtn = document.getElementById("project-file-back");
  var askBtn = document.getElementById("project-file-ask");
  var inputEl = document.getElementById("chat-input");

  var KEYWORDS = {
    python: ["def", "class", "return", "import", "from", "as", "if", "elif", "else", "for", "while", "try", "except", "finally", "with", "lambda", "yield", "None", "True", "False", "and", "or", "not", "in", "is", "pass", "raise", "async", "await", "self"],
    javascript: ["function", "const", "let", "var", "return", "if", "else", "for", "while", "class", "extends", "new", "import", "export", "default", "from", "async", "await", "try", "catch", "finally", "throw", "typeof", "null", "undefined", "true", "false", "this"],
    typescript: ["function", "const", "let", "var", "return", "if", "else", "for", "while", "class", "interface", "type", "enum", "extends", "implements", "new", "import", "export", "default", "from", "async", "await", "try", "catch", "finally", "throw", "public", "private", "protected", "readonly", "null", "undefined", "true", "false", "this"],
    rust: ["fn", "let", "mut", "pub", "impl", "struct", "enum", "trait", "use", "mod", "match", "if", "else", "for", "while", "loop", "return", "self", "Self", "as", "const", "static", "async", "await", "move", "where", "true", "false"],
    json: ["true", "false", "null"],
    default: [],
  };

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  function escapeRegExp(text) {
    return text.replace(/[.*+?^${}()|[\]\\]/g, "\\$&");
  }

  function humanSize(bytes) {
    var value = Number(bytes) || 0;
    if (value < 1024) return value + " B";
    if (value < 1024 * 1024) return (value / 1024).toFixed(1) + " KB";
    return (value / (1024 * 1024)).toFixed(1) + " MB";
  }

  function formatDate(iso) {
    if (!iso) return "";
    var date = new Date(iso);
    return isNaN(date.getTime()) ? "" : date.toLocaleDateString();
  }

  // Escapes all non-token text and wraps comments/strings/keywords/numbers.
  function highlight(code, language) {
    var lang = (language || "").toLowerCase();
    var keywords = KEYWORDS[lang] || KEYWORDS.default;
    var kw = keywords.length ? keywords.map(escapeRegExp).join("|") : "$^";
    var pattern = new RegExp(
      "\\/\\/[^\\n]*|#[^\\n]*|\\/\\*[\\s\\S]*?\\*\\/" +
        "|\"(?:\\\\.|[^\"\\\\])*\"|'(?:\\\\.|[^'\\\\])*'|`(?:\\\\.|[^`\\\\])*`" +
        "|\\b(?:" + kw + ")\\b" +
        "|\\b\\d+(?:\\.\\d+)?\\b",
      "g",
    );

    var out = "";
    var last = 0;
    var match;
    while ((match = pattern.exec(code)) !== null) {
      out += escapeHtml(code.slice(last, match.index));
      var token = match[0];
      var cls = "tok-keyword";
      if (/^(\/\/|#|\/\*)/.test(token)) cls = "tok-comment";
      else if (/^["'`]/.test(token)) cls = "tok-string";
      else if (/^\d/.test(token)) cls = "tok-number";
      out += '<span class="' + cls + '">' + escapeHtml(token) + "</span>";
      last = match.index + token.length;
      if (token.length === 0) pattern.lastIndex += 1; // guard against zero-width
    }
    out += escapeHtml(code.slice(last));
    return out;
  }

  function get(url) {
    return fetch(url, { headers: { Accept: "application/json" } }).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) {
          throw new Error((data && data.error) || "Request failed (" + response.status + ").");
        }
        return data;
      });
    });
  }

  function renderWorkspaces(workspaces) {
    treeEl.innerHTML = "";
    if (!workspaces.length) {
      treeEl.innerHTML =
        '<p class="sidebar-empty">No workspaces yet. Import a project in Workspaces to browse files here.</p>';
      return;
    }

    workspaces.forEach(function (workspace) {
      var wsEl = document.createElement("details");
      wsEl.className = "file-tree-workspace";
      wsEl.open = true;
      var wsSummary = document.createElement("summary");
      wsSummary.textContent = workspace.name;
      wsEl.appendChild(wsSummary);

      var body = document.createElement("div");
      if (!workspace.projects.length) {
        body.innerHTML = '<p class="sidebar-empty">No projects in this workspace.</p>';
      }

      workspace.projects.forEach(function (project) {
        var pEl = document.createElement("details");
        pEl.className = "file-tree-project";
        pEl.open = true;
        var pSummary = document.createElement("summary");
        pSummary.textContent = project.name;
        if (project.status !== "ready") {
          var tag = document.createElement("span");
          tag.className = "tag";
          tag.textContent = project.status;
          pSummary.appendChild(document.createTextNode(" "));
          pSummary.appendChild(tag);
        }
        pEl.appendChild(pSummary);

        var filesEl = document.createElement("ul");
        filesEl.className = "file-tree-files";
        if (project.status !== "ready") {
          filesEl.innerHTML = '<li class="sidebar-empty">Indexing…</li>';
        } else if (!project.files.length) {
          filesEl.innerHTML = '<li class="sidebar-empty">No files.</li>';
        } else {
          project.files.forEach(function (file) {
            var li = document.createElement("li");
            li.className = "file-tree-file";
            li.dataset.project = project.id;
            li.dataset.path = file.path;
            li.dataset.language = file.language || "";
            li.innerHTML =
              '<span class="file-tree-name">' + escapeHtml(file.path) + "</span>" +
              '<span class="file-tree-meta">' + humanSize(file.size) +
              (file.created_at ? " · " + formatDate(file.created_at) : "") + "</span>";
            filesEl.appendChild(li);
          });
          if (project.truncated) {
            var note = document.createElement("li");
            note.className = "sidebar-empty";
            note.textContent = "Showing the first " + project.files.length + " files.";
            filesEl.appendChild(note);
          }
        }
        pEl.appendChild(filesEl);
        body.appendChild(pEl);
      });

      wsEl.appendChild(body);
      treeEl.appendChild(wsEl);
    });
  }

  function loadTree() {
    get("/chat/api/project-files")
      .then(renderWorkspaces)
      .catch(function (error) {
        treeEl.innerHTML = '<p class="sidebar-empty">' + escapeHtml(error.message) + "</p>";
      });
  }

  function showFile(projectId, path) {
    viewerEl.hidden = false;
    treeEl.hidden = true;
    metaEl.textContent = path;
    contentEl.textContent = "Loading…";

    get(
      "/workspaces/api/projects/" + projectId + "/file?path=" + encodeURIComponent(path),
    )
      .then(function (data) {
        metaEl.textContent =
          data.path + " · " + humanSize(data.size) + (data.language ? " · " + data.language : "");
        if (data.is_binary || !data.searchable) {
          contentEl.textContent = "This file is binary or too large to display.";
        } else {
          contentEl.innerHTML = highlight(data.content || "", data.language);
        }
      })
      .catch(function (error) {
        contentEl.textContent = error.message;
      });

    askBtn.dataset.project = projectId;
    askBtn.dataset.path = path;
  }

  function hideViewer() {
    viewerEl.hidden = true;
    treeEl.hidden = false;
  }

  treeEl.addEventListener("click", function (event) {
    var item = event.target.closest(".file-tree-file");
    if (!item) return;
    showFile(item.dataset.project, item.dataset.path);
  });

  backBtn.addEventListener("click", hideViewer);

  askBtn.addEventListener("click", function () {
    var path = askBtn.dataset.path;
    if (!path || !inputEl) return;
    inputEl.value = "Explain the file `" + path + "` and summarize what it does.";
    inputEl.focus();
  });

  document.addEventListener("DOMContentLoaded", loadTree);
})();
