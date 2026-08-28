// AI Code Assistant — project explorer
// Lazy file tree, file viewer, project search, bounded AI chat (SSE), project
// analyses, and the health dashboard.

(function () {
  "use strict";

  var PROJECT_ID = null;
  var treeEl = document.getElementById("project-tree");
  var viewerEl = document.getElementById("file-viewer");
  var chatMessagesEl = document.getElementById("project-chat-messages");
  var chatInputEl = document.getElementById("project-chat-input");
  var chatSendBtn = document.getElementById("project-chat-send");
  var streaming = false;
  var chatLoaded = false;

  function getCsrf() {
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) return meta.content;
    var input = document.querySelector('input[name="csrf_token"]');
    return input ? input.value : "";
  }

  function flashError(message) {
    var el = document.createElement("div");
    el.className = "flash flash-error";
    el.textContent = message;
    var main = document.querySelector(".main-content");
    (main || document.body).prepend(el);
  }

  function api(url, options) {
    options = options || {};
    options.headers = Object.assign({}, options.headers || {}, {
      "X-CSRFToken": getCsrf(),
    });
    return fetch(url, options).then(function (response) {
      return response.json().then(function (data) {
        if (!response.ok) {
          var error = new Error(data && data.error ? data.error : "Request failed (" + response.status + ").");
          throw error;
        }
        return data;
      });
    });
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text == null ? "" : String(text);
    return div.innerHTML;
  }

  function renderInline(text) {
    var escaped = escapeHtml(text);
    return escaped
      .replace(/\*\*([^*]+)\*\*/g, "<strong>$1</strong>")
      .replace(/\*([^*]+)\*/g, "<em>$1</em>")
      .replace(/`([^`]+)`/g, '<code class="inline-code">$1</code>')
      .replace(/\[([^\]]+)\]\(([^)]+)\)/g, '<a href="$2" target="_blank" rel="noopener">$1</a>');
  }

  function renderMarkdown(text) {
    var lines = String(text).split("\n");
    var html = "";
    var inCode = false;
    var codeLang = "";
    var codeLines = [];
    var listOpen = false;

    function flushList() {
      if (listOpen) {
        html += "</ul>\n";
        listOpen = false;
      }
    }

    lines.forEach(function (line) {
      var codeMatch = line.match(/^```(\w*)/);
      if (codeMatch) {
        flushList();
        if (inCode) {
          html += '<pre class="code-block"><code class="language-' + escapeHtml(codeLang) + '">' +
            escapeHtml(codeLines.join("\n")) + "</code></pre>\n";
          inCode = false;
          codeLines = [];
        } else {
          inCode = true;
          codeLang = codeMatch[1] || "";
        }
        return;
      }
      if (inCode) {
        codeLines.push(line);
        return;
      }
      if (/^\s*[-*]\s+/.test(line)) {
        if (!listOpen) {
          html += "<ul>\n";
          listOpen = true;
        }
        html += "<li>" + renderInline(line.replace(/^\s*[-*]\s+/, "")) + "</li>\n";
        return;
      }
      flushList();
      if (/^#{1,4}\s/.test(line)) {
        var level = line.match(/^(#{1,4})\s/)[1].length;
        html += "<h" + level + ">" + renderInline(line.replace(/^#{1,4}\s/, "")) + "</h" + level + ">\n";
      } else if (/^\s*$/.test(line)) {
        html += "<br>\n";
      } else {
        html += "<p>" + renderInline(line) + "</p>\n";
      }
    });
    flushList();
    if (inCode) {
      html += '<pre class="code-block"><code class="language-' + escapeHtml(codeLang) + '">' +
        escapeHtml(codeLines.join("\n")) + "</code></pre>\n";
    }
    return html;
  }

  function renderAnalysis(container, text) {
    var html = escapeHtml(text)
      .replace(/\[CONFIRMED\]/g, '<span class="tag tag-confirmed">[CONFIRMED]</span>')
      .replace(/\[SUGGESTION\]/g, '<span class="tag tag-suggestion">[SUGGESTION]</span>')
      .replace(/\r?\n/g, "<br>");
    container.innerHTML = '<div class="analysis-text">' + html + "</div>";
  }

  function scrollChat() {
    chatMessagesEl.scrollTop = chatMessagesEl.scrollHeight;
  }

  // ------------------------------------------------------------------ tabs

  function switchTab(name) {
    document.querySelectorAll("#project-tabs .repo-tab").forEach(function (tab) {
      tab.classList.toggle("active", tab.dataset.tab === name);
    });
    ["files", "search", "chat", "analysis", "stats", "stellar", "discussion"].forEach(function (key) {
      document.getElementById("tab-" + key).hidden = key !== name;
    });
    if (name === "chat" && !chatLoaded) loadChatHistory();
    if (name === "stats") loadStats();
    if (name === "stellar") loadStellar();
    if (name === "discussion") loadComments();
  }

  // ------------------------------------------------------------------ tree

  function renderDir(fullPath, data, containerUl) {
    containerUl.innerHTML = "";
    data.directories.forEach(function (name) {
      var childPath = fullPath ? fullPath + "/" + name : name;
      var li = document.createElement("li");
      li.className = "tree-dir";
      li.innerHTML =
        '<button class="tree-toggle" type="button">▸</button>' +
        '<button class="tree-dir-label" type="button">' + escapeHtml(name) + "</button>" +
        '<ul class="tree-children" hidden></ul>';
      li.querySelector(".tree-toggle").addEventListener("click", function () {
        toggleDir(li, childPath);
      });
      li.querySelector(".tree-dir-label").addEventListener("click", function () {
        toggleDir(li, childPath);
      });
      containerUl.appendChild(li);
    });
    data.files.forEach(function (file) {
      var li = document.createElement("li");
      li.className = "tree-file";
      li.innerHTML =
        '<button class="tree-file-label" type="button">' +
        escapeHtml(file.path.split("/").pop()) +
        '<span class="tree-file-size">' + humanSize(file.size) + "</span></button>";
      li.addEventListener("click", function () {
        loadFile(file.path);
      });
      containerUl.appendChild(li);
    });
    if (!data.directories.length && !data.files.length) {
      containerUl.innerHTML = '<li class="tree-empty">(empty)</li>';
    }
  }

  function humanSize(size) {
    if (!size && size !== 0) return "";
    if (size < 1024) return size + " B";
    if (size < 1048576) return (size / 1024).toFixed(1) + " KB";
    return (size / 1048576).toFixed(1) + " MB";
  }

  function loadDir(fullPath, containerUl) {
    containerUl.innerHTML = '<li class="tree-empty">Loading...</li>';
    var url = "/workspaces/api/projects/" + PROJECT_ID + "/tree";
    if (fullPath) url += "?path=" + encodeURIComponent(fullPath);
    api(url)
      .then(function (data) {
        renderDir(fullPath, data, containerUl);
      })
      .catch(function (error) {
        containerUl.innerHTML = '<li class="tree-empty">' + escapeHtml(error.message) + "</li>";
      });
  }

  function toggleDir(li, fullPath) {
    var children = li.querySelector(".tree-children");
    var toggle = li.querySelector(".tree-toggle");
    if (children.hidden) {
      if (li.dataset.loaded !== "1") {
        loadDir(fullPath, children);
        li.dataset.loaded = "1";
      }
      children.hidden = false;
      toggle.textContent = "▾";
    } else {
      children.hidden = true;
      toggle.textContent = "▸";
    }
  }

  // ----------------------------------------------------------------- file

  function loadFile(path) {
    switchTab("files");
    viewerEl.innerHTML = '<p class="sidebar-empty">Loading file...</p>';
    api("/workspaces/api/projects/" + PROJECT_ID + "/file?path=" + encodeURIComponent(path))
      .then(function (data) {
        if (!data.searchable) {
          viewerEl.innerHTML =
            '<p class="repo-meta">' + escapeHtml(data.path) +
            " — this file is binary or too large to display/search (size: " + humanSize(data.size) + ").</p>";
          return;
        }
        viewerEl.innerHTML =
          '<div class="file-viewer-header">' +
          '<code>' + escapeHtml(data.path) + "</code>" +
          '<span class="tag">' + escapeHtml(data.language || "text") + "</span>" +
          "</div>" +
          '<pre class="code-view">' + escapeHtml(data.content) + "</pre>";
      })
      .catch(function (error) {
        viewerEl.innerHTML = '<p class="sidebar-empty">' + escapeHtml(error.message) + "</p>";
      });
  }

  // ---------------------------------------------------------------- search

  function runSearch() {
    var query = document.getElementById("search-query").value.trim();
    var caseSensitive = document.getElementById("search-case").checked;
    var resultsEl = document.getElementById("search-results");
    if (!query) {
      resultsEl.innerHTML = '<p class="sidebar-empty">Enter a query to search the project.</p>';
      return;
    }
    resultsEl.innerHTML = '<p class="sidebar-empty">Searching...</p>';
    var url = "/workspaces/api/projects/" + PROJECT_ID + "/search?q=" + encodeURIComponent(query);
    if (caseSensitive) url += "&case=1";
    api(url)
      .then(function (data) {
        resultsEl.innerHTML = "";
        if (!data.results.length) {
          resultsEl.innerHTML = '<p class="sidebar-empty">No matches found.</p>';
          return;
        }
        data.results.forEach(function (result) {
          var row = document.createElement("div");
          row.className = "search-result";
          var meta = result.matched === "path" ? "file name" : "contents";
          row.innerHTML =
            '<div class="search-result-path"><code>' + escapeHtml(result.path) + "</code>" +
            '<span class="tag">' + escapeHtml(meta) + "</span></div>" +
            (result.snippet ? '<p class="search-result-snippet">' + escapeHtml(result.snippet) + "</p>" : "");
          row.addEventListener("click", function () {
            loadFile(result.path);
          });
          resultsEl.appendChild(row);
        });
      })
      .catch(function (error) {
        resultsEl.innerHTML = '<p class="sidebar-empty">' + escapeHtml(error.message) + "</p>";
      });
  }

  // ----------------------------------------------------------------- chat

  function addChatMessage(role, content, asMarkdown) {
    var el = document.createElement("div");
    el.className = "chat-message chat-" + role;
    var label = role === "user" ? "You" : "Assistant";
    el.innerHTML =
      '<div class="message-header">' + escapeHtml(label) + "</div>" +
      '<div class="message-body">' +
      (role === "user" || !asMarkdown ? escapeHtml(content) : renderMarkdown(content)) +
      "</div>";
    chatMessagesEl.appendChild(el);
    scrollChat();
    return el;
  }

  function addTypingIndicator() {
    var el = document.createElement("div");
    el.className = "chat-message chat-assistant typing";
    el.innerHTML = '<div class="message-header">Assistant</div><div class="typing-indicator"><span></span><span></span><span></span></div>';
    chatMessagesEl.appendChild(el);
    scrollChat();
    return el;
  }

  function loadChatHistory() {
    chatLoaded = true;
    api("/workspaces/api/projects/" + PROJECT_ID + "/messages")
      .then(function (messages) {
        chatMessagesEl.innerHTML = "";
        messages.forEach(function (message) {
          addChatMessage(message.role, message.content, true);
        });
        if (!messages.length) {
          chatMessagesEl.innerHTML =
            '<div class="chat-placeholder"><p>Ask questions about this project. The assistant retrieves a bounded slice of context before answering.</p></div>';
        }
      })
      .catch(function (error) {
        flashError(error.message);
      });
  }

  async function startChat() {
    var content = chatInputEl.value.trim();
    if (!content || streaming) return;
    chatInputEl.value = "";
    chatSendBtn.disabled = true;
    streaming = true;
    addChatMessage("user", content);

    var typing = addTypingIndicator();
    var bodyEl = typing.querySelector(".typing-indicator");

    try {
      var response = await fetch("/workspaces/api/projects/" + PROJECT_ID + "/chat/stream", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": getCsrf(),
        },
        body: JSON.stringify({ content: content }),
      });

      if (!response.ok) {
        var errData = null;
        try {
          errData = await response.json();
        } catch (e) {
          errData = null;
        }
        throw new Error(errData && errData.error ? errData.error : "Stream failed (" + response.status + ").");
      }

      typing.classList.add("streaming");
      bodyEl.style.display = "none";
      var streamBody = document.createElement("div");
      streamBody.className = "message-body";
      typing.appendChild(streamBody);

      var reader = response.body.getReader();
      var decoder = new TextDecoder();
      var buffer = "";
      var fullText = "";

      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        var events = buffer.split("\n\n");
        buffer = events.pop();
        events.forEach(function (event) {
          var line = event.split("\n")[0];
          if (!line.startsWith("data: ")) return;
          var payload = null;
          try {
            payload = JSON.parse(line.slice(6));
          } catch (e) {
            return;
          }
          if (payload.type === "token") {
            fullText += payload.content;
            streamBody.innerHTML = renderMarkdown(fullText);
            scrollChat();
          } else if (payload.type === "error") {
            flashError(payload.error);
          } else if (payload.type === "done") {
            streamBody.innerHTML = renderMarkdown(payload.message.content);
            scrollChat();
          }
        });
      }
    } catch (error) {
      flashError(error.message);
    } finally {
      typing.classList.remove("typing", "streaming");
      if (!typing.querySelector(".message-body") || !typing.querySelector(".message-body").textContent) {
        typing.remove();
      }
      chatSendBtn.disabled = false;
      streaming = false;
    }
  }

  // ------------------------------------------------------------- analysis

  function runAnalysis(kind) {
    var output = document.getElementById("analysis-output");
    output.innerHTML = '<p class="sidebar-empty">Analyzing project (bounded context), please wait...</p>';
    api("/workspaces/api/projects/" + PROJECT_ID + "/analyze", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ kind: kind }),
    })
      .then(function (data) {
        renderAnalysis(output, data.analysis);
      })
      .catch(function (error) {
        output.innerHTML = '<p class="sidebar-empty">' + escapeHtml(error.message) + "</p>";
      });
  }

  // ---------------------------------------------------------------- stats

  function loadStats() {
    var output = document.getElementById("stats-output");
    api("/workspaces/api/projects/" + PROJECT_ID + "/stats")
      .then(function (data) {
        var project = data.project;
        var html = "";
        html += '<div class="metric-grid">';
        html += metric("Files", data.file_count);
        html += metric("Total size", humanSize(data.total_size_bytes));
        html += metric("Searchable", data.searchable_file_count);
        html += metric("Tests", data.test_file_count);
        html += metric("Docs", data.doc_file_count);
        html += metric("Dependencies", data.dependency_count);
        if (data.index_duration_seconds !== null && data.index_duration_seconds !== undefined) {
          html += metric("Index time", data.index_duration_seconds + "s");
        }
        html += metric("Status", project.status);
        html += "</div>";

        if (data.languages.length) {
          html += '<h3 class="metric-title">Languages</h3><ul class="metric-list">';
          data.languages.forEach(function (pair) {
            html += "<li><code>" + escapeHtml(pair[0]) + "</code> — " + pair[1] + "</li>";
          });
          html += "</ul>";
        }
        if (data.manifest_files.length) {
          html += '<h3 class="metric-title">Dependency manifests</h3><ul class="metric-list">';
          data.manifest_files.forEach(function (path) {
            html += "<li><code>" + escapeHtml(path) + "</code></li>";
          });
          html += "</ul>";
        }
        output.innerHTML = html;
      })
      .catch(function (error) {
        output.innerHTML = '<p class="sidebar-empty">' + escapeHtml(error.message) + "</p>";
      });
  }

  function metric(label, value) {
    return '<div class="metric-card"><span class="metric-value">' + escapeHtml(value) +
      '</span><span class="metric-label">' + escapeHtml(label) + "</div>";
  }

  // -------------------------------------------------------------- stellar

  function loadStellar() {
    var output = document.getElementById("stellar-output");
    api("/workspaces/api/projects/" + PROJECT_ID + "/stellar")
      .then(function (data) {
        var html = "";
        var badge = data.is_stellar
          ? '<span class="tag tag-confirmed">Stellar project</span>'
          : '<span class="tag">Not a Stellar project</span>';
        html += '<div class="stellar-panel">';
        html += "<div><h3>Stellar / Soroban detection</h3>" + badge + "</div>";
        html += "<ul class=\"metric-list\">";
        html += "<li><code>Confidence</code> — " + escapeHtml(data.confidence) + "</li>";
        html += "<li><code>Stellar</code> — " + (data.is_stellar ? "yes" : "no") + "</li>";
        html += "<li><code>Soroban (smart contracts)</code> — " + (data.is_soroban ? "yes" : "no") + "</li>";
        if (data.network && data.network.network) {
          html += "<li><code>Network hint</code> — " + escapeHtml(data.network.network) + "</li>";
        }
        html += "</ul>";

        if (data.evidence && data.evidence.length) {
          html += '<h3 class="metric-title">Evidence</h3><ul class="metric-list">';
          data.evidence.forEach(function (line) {
            html += "<li>" + escapeHtml(line) + "</li>";
          });
          html += "</ul>";
        }
        if (data.relevant_files && data.relevant_files.length) {
          html += '<h3 class="metric-title">Relevant Stellar files</h3><ul class="metric-list">';
          data.relevant_files.forEach(function (path) {
            html += "<li><code>" + escapeHtml(path) + "</code></li>";
          });
          html += "</ul>";
        }
        if (data.is_stellar) {
          html +=
            '<p class="field-hint">Run <b>Stellar</b> or <b>Stellar Security</b> ' +
            "analysis from the Analysis tab for an AI review grounded in these files.</p>";
        } else {
          html +=
            '<p class="field-hint">This project shows no Stellar/Soroban signals. ' +
            "Plain Rust, Python, JavaScript, and other projects are never classified " +
            "as Stellar without concrete evidence.</p>";
        }
        html += "</div>";
        output.innerHTML = html;
      })
      .catch(function (error) {
        output.innerHTML = '<p class="sidebar-empty">' + escapeHtml(error.message) + "</p>";
      });
  }

  // ------------------------------------------------------------ discussion

  var commentsLoaded = false;

  function formatCommentTime(iso) {
    if (!iso) return "";
    return new Date(iso).toLocaleString();
  }

  function loadComments() {
    var list = document.getElementById("comment-list");
    if (commentsLoaded) return;
    api("/workspaces/api/projects/" + PROJECT_ID + "/comments?per_page=100")
      .then(function (data) {
        commentsLoaded = true;
        if (!data.items.length) {
          list.innerHTML = '<p class="sidebar-empty">No comments yet. Start the discussion.</p>';
          return;
        }
        var html = "";
        data.items.forEach(function (comment) {
          html +=
            '<div class="notification-row">' +
            '<div class="notification-info">' +
            "<div>" +
            '<div class="notification-title">' + escapeHtml(comment.author_username || "deleted user") + "</div>" +
            '<div class="notification-meta">' + formatCommentTime(comment.created_at) + "</div>" +
            '<div style="margin-top:6px;">' + renderInline(comment.content) + "</div>" +
            "</div>" +
            "</div>" +
            "</div>";
        });
        list.innerHTML = html;
      })
      .catch(function (error) {
        list.innerHTML = '<p class="sidebar-empty">' + escapeHtml(error.message) + "</p>";
      });
  }

  function postComment() {
    var input = document.getElementById("comment-input");
    var btn = document.getElementById("comment-send");
    var content = input.value.trim();
    if (!content) {
      input.focus();
      return;
    }
    btn.disabled = true;
    api("/workspaces/api/projects/" + PROJECT_ID + "/comments", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: content }),
    })
      .then(function () {
        input.value = "";
        commentsLoaded = false;
        loadComments();
      })
      .catch(function (error) {
        flashError(error.message);
      })
      .then(function () {
        btn.disabled = false;
      });
  }

  // ----------------------------------------------------------------- init

  document.addEventListener("DOMContentLoaded", function () {
    var parts = window.location.pathname.split("/").filter(Boolean);
    PROJECT_ID = parseInt(parts[parts.length - 1], 10) || 0;

    var rootUl = document.createElement("ul");
    rootUl.className = "tree-children";
    treeEl.appendChild(rootUl);
    loadDir("", rootUl);

    document.getElementById("refresh-tree").addEventListener("click", function () {
      treeEl.innerHTML = "";
      var ul = document.createElement("ul");
      ul.className = "tree-children";
      treeEl.appendChild(ul);
      loadDir("", ul);
    });

    document.querySelectorAll("#project-tabs .repo-tab").forEach(function (tab) {
      tab.addEventListener("click", function () {
        switchTab(tab.dataset.tab);
      });
    });

    document.getElementById("search-run").addEventListener("click", runSearch);
    document.getElementById("search-query").addEventListener("keydown", function (event) {
      if (event.key === "Enter") runSearch();
    });

    chatSendBtn.addEventListener("click", startChat);
    chatInputEl.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        startChat();
      }
    });

    document.querySelectorAll(".analysis-btn").forEach(function (btn) {
      btn.addEventListener("click", function () {
        document.querySelectorAll(".analysis-btn").forEach(function (b) {
          b.classList.toggle("active", b === btn);
        });
        runAnalysis(btn.dataset.kind);
      });
    });

    document.getElementById("comment-send").addEventListener("click", postComment);
    document.getElementById("comment-input").addEventListener("keydown", function (event) {
      if (event.key === "Enter" && (event.ctrlKey || event.metaKey)) {
        event.preventDefault();
        postComment();
      }
    });

    document.getElementById("delete-project").addEventListener("click", function () {
      if (!confirm("Delete this project and its indexed files?")) return;
      api("/workspaces/api/projects/" + PROJECT_ID, { method: "DELETE" })
        .then(function () {
          var parts2 = window.location.pathname.split("/").filter(Boolean);
          window.location.href = "/workspaces/" + parts2[1];
        })
        .catch(function (error) {
          flashError(error.message);
        });
    });
  });
})();
