// AI Code Assistant — project explorer
// Lazy file tree, file viewer, project search, bounded AI chat (SSE), project
// analyses, and the health dashboard.

(function () {
  "use strict";

  var PROJECT_ID = null;
  var treeEl = document.getElementById("project-tree");
  var viewerEl = document.getElementById("file-viewer");
  var breadcrumbsEl = document.getElementById("file-breadcrumbs");
  var currentPath = "";
  var chatMessagesEl = document.getElementById("project-chat-messages");
  var chatSessionListEl = document.getElementById("chat-session-list");
  var newChatSessionBtn = document.getElementById("new-chat-session");
  var chatInputEl = document.getElementById("project-chat-input");
  var chatSendBtn = document.getElementById("project-chat-send");
  var chatAttachmentsEl = document.getElementById("project-chat-attachments");
  var reviewSummaryEl = document.getElementById("review-summary");
  var streaming = false;
  var chatLoaded = false;
  var activeSessionId = null;
  var pendingAttachments = [];

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
    ["files", "search", "chat", "analysis", "stats", "reviews", "stellar", "discussion"].forEach(function (key) {
      document.getElementById("tab-" + key).hidden = key !== name;
    });
    if (name === "chat") {
      if (!chatLoaded) loadChatHistory();
      loadReviewSummary();
    }
    if (name === "stats") loadStats();
    if (name === "reviews") loadProjectReviews();
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
        if (pendingAttachments.indexOf(file.path) === -1) {
          pendingAttachments.push(file.path);
          renderChatAttachments();
        }
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
      setCurrentPath(fullPath);
    } else {
      children.hidden = true;
      toggle.textContent = "▸";
    }
  }

  // ------------------------------------------------------------ breadcrumbs

  function renderBreadcrumbs(path) {
    if (!breadcrumbsEl) return;
    var parts = (path || "").split("/").filter(Boolean);
    var html = '<button type="button" class="file-crumb" data-path="">Project</button>';
    var accumulated = "";
    parts.forEach(function (segment, index) {
      accumulated = accumulated ? accumulated + "/" + segment : segment;
      html += '<span class="file-crumb-sep">/</span>';
      if (index === parts.length - 1) {
        html += '<span class="file-crumb-current">' + escapeHtml(segment) + "</span>";
      } else {
        html +=
          '<button type="button" class="file-crumb" data-path="' +
          escapeHtml(accumulated) +
          '">' +
          escapeHtml(segment) +
          "</button>";
      }
    });
    breadcrumbsEl.innerHTML = html;
  }

  function setCurrentPath(path) {
    currentPath = path || "";
    renderBreadcrumbs(currentPath);
  }

  function navigateToDir(path) {
    var ul = document.createElement("ul");
    ul.className = "tree-children";
    treeEl.innerHTML = "";
    treeEl.appendChild(ul);
    loadDir(path || "", ul);
    setCurrentPath(path || "");
    switchTab("files");
  }

  // ----------------------------------------------------------------- file

  function loadFile(path) {
    switchTab("files");
    setCurrentPath(path);
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

  function renderChatAttachments() {
    if (!chatAttachmentsEl) return;
    chatAttachmentsEl.innerHTML = pendingAttachments.map(function (path, index) {
      return '<button type="button" class="chat-attachment" data-index="' + index + '" title="Remove attachment">' +
        escapeHtml(path) + " x</button>";
    }).join("");
    chatAttachmentsEl.querySelectorAll(".chat-attachment").forEach(function (button) {
      button.addEventListener("click", function () {
        pendingAttachments.splice(parseInt(button.dataset.index, 10), 1);
        renderChatAttachments();
      });
    });
  }

  function runSearch() {
    var query = document.getElementById("search-query").value.trim();
    var caseSensitive = document.getElementById("search-case").checked;
    var regex = document.getElementById("search-regex").checked;
    var scopeEl = document.getElementById("search-scope");
    var languageEl = document.getElementById("search-language");
    var resultsEl = document.getElementById("search-results");
    if (!query) {
      resultsEl.innerHTML = '<p class="sidebar-empty">Enter a query to search the project.</p>';
      return;
    }
    resultsEl.innerHTML = '<p class="sidebar-empty">Searching...</p>';
    var params = ["q=" + encodeURIComponent(query)];
    if (caseSensitive) params.push("case=1");
    if (regex) params.push("regex=1");
    if (scopeEl && scopeEl.value && scopeEl.value !== "all") {
      params.push("scope=" + encodeURIComponent(scopeEl.value));
    }
    var language = languageEl ? languageEl.value.trim() : "";
    if (language) params.push("language=" + encodeURIComponent(language));
    var url = "/workspaces/api/projects/" + PROJECT_ID + "/search?" + params.join("&");
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

  function addChatMessage(role, content, asMarkdown, messageId) {
    var el = document.createElement("div");
    el.className = "chat-message chat-" + role;
    var label = role === "user" ? "You" : "Assistant";
    el.innerHTML =
      '<div class="message-header">' + escapeHtml(label) + "</div>" +
      '<div class="message-body">' +
      (role === "user" || !asMarkdown ? escapeHtml(content) : renderMarkdown(content)) +
      "</div>";
    if (role === "assistant" && messageId != null) {
      el.__reviewContent = content;
      attachReviewFooter(el, messageId);
    }
    chatMessagesEl.appendChild(el);
    scrollChat();
    return el;
  }

  function attachReviewFooter(messageEl, messageId) {
    if (messageEl.querySelector(".message-review")) return;
    messageEl.setAttribute("data-message-id", messageId);
    var footer = document.createElement("div");
    footer.className = "message-review";
    footer.innerHTML =
      '<button class="btn btn-ghost btn-sm message-review-toggle" type="button">' +
      "Inline review comments</button>" +
      '<div class="message-review-panel" hidden></div>';
    messageEl.appendChild(footer);
  }

  function addTypingIndicator() {
    var el = document.createElement("div");
    el.className = "chat-message chat-assistant typing";
    el.innerHTML = '<div class="message-header">Assistant</div><div class="typing-indicator"><span></span><span></span><span></span></div>';
    chatMessagesEl.appendChild(el);
    scrollChat();
    return el;
  }

  function renderChatSessions(sessions) {
    chatSessionListEl.innerHTML = "";
    sessions.forEach(function (session) {
      var button = document.createElement("button");
      button.type = "button";
      button.className = "chat-session" + (session.id === activeSessionId ? " active" : "");
      button.textContent = session.title;
      button.title = session.title;
      button.addEventListener("click", function () {
        if (session.id === activeSessionId) return;
        activeSessionId = session.id;
        renderChatSessions(sessions);
        loadChatMessages();
      });
      chatSessionListEl.appendChild(button);
    });
  }

  function loadChatSessions() {
    return api("/workspaces/api/projects/" + PROJECT_ID + "/sessions")
      .then(function (sessions) {
        if (!activeSessionId && sessions.length) activeSessionId = sessions[0].id;
        renderChatSessions(sessions);
      });
  }

  function loadChatMessages() {
    var query = activeSessionId ? "?session_id=" + activeSessionId : "";
    return api("/workspaces/api/projects/" + PROJECT_ID + "/messages" + query)
      .then(function (messages) {
        chatMessagesEl.innerHTML = "";
        messages.forEach(function (message) {
          addChatMessage(
            message.role,
            message.content,
            true,
            message.role === "assistant" ? message.id : null
          );
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

  function loadChatHistory() {
    chatLoaded = true;
    loadChatSessions().then(loadChatMessages).catch(function (error) {
      flashError(error.message);
    });
  }

  function createChatSession() {
    var title = window.prompt("Session title");
    if (!title || !title.trim()) return;
    api("/workspaces/api/projects/" + PROJECT_ID + "/sessions", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ title: title.trim() }),
    }).then(function (session) {
      activeSessionId = session.id;
      return loadChatSessions().then(loadChatMessages);
    }).catch(function (error) {
      flashError(error.message);
    });
  }

  async function startChat() {
    var content = chatInputEl.value.trim();
    if (!content || streaming) return;
    var attachments = pendingAttachments.slice();
    pendingAttachments = [];
    renderChatAttachments();
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
        body: JSON.stringify({ content: content, session_id: activeSessionId, attachments: attachments }),
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
            loadChatSessions();
          } else if (payload.type === "error") {
            flashError(payload.error);
          } else if (payload.type === "done") {
            streamBody.innerHTML = renderMarkdown(payload.message.content);
            if (payload.message && payload.message.id != null) {
              typing.__reviewContent = payload.message.content;
              attachReviewFooter(typing, payload.message.id);
            }
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
        var coverage = data.coverage_estimate;
        if (coverage) {
          html += '<h3 class="metric-title">Coverage (estimated)</h3>';
          html += '<p class="field-hint">' + escapeHtml(coverage.note) + "</p>";
          html += '<div class="metric-grid">';
          html += metric("Estimate", (coverage.ratio * 100).toFixed(1) + "%");
          html += metric("Test files", coverage.test_file_count);
          html += metric("Source files", coverage.source_file_count);
          html += metric("Signal", coverage.label);
          html += "</div>";
        }
        if (data.ci_files && data.ci_files.length) {
          html += '<h3 class="metric-title">CI configuration</h3><ul class="metric-list">';
          data.ci_files.forEach(function (path) {
            html += "<li><code>" + escapeHtml(path) + "</code></li>";
          });
          html += "</ul>";
        } else {
          html += '<h3 class="metric-title">CI configuration</h3>';
          html += '<p class="sidebar-empty">No CI configuration detected.</p>';
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

  // --------------------------------------------------------------- reviews

  var reviewsLoaded = false;

  function reviewStatusTag(review) {
    var cls = review.status === "completed" ? "tag-public" :
      review.status === "failed" ? "tag-private" : "";
    return '<span class="tag ' + cls + '">' + escapeHtml(review.status) + "</span>";
  }

  function loadProjectReviews() {
    var output = document.getElementById("project-reviews");
    if (!output || reviewsLoaded) return;
    reviewsLoaded = true;
    api("/reviews/api/reviews?project_id=" + PROJECT_ID)
      .then(function (reviews) {
        if (!reviews.length) {
          output.innerHTML =
            '<p class="sidebar-empty">No reviews yet. Open the review runner to run one.</p>';
          return;
        }
        var html = "";
        reviews.forEach(function (review) {
          html +=
            '<a class="issue-item" href="/reviews/' + review.id + '">' +
            '<div class="issue-item-title">' + reviewStatusTag(review) + " " +
            escapeHtml(review.kind) + " review</div>" +
            '<div class="issue-item-sub">' +
            (review.findings_count || 0) + " findings &middot; " +
            escapeHtml(review.created_at ? new Date(review.created_at).toLocaleString() : "") +
            "</div></a>";
        });
        output.innerHTML = html;
      })
      .catch(function (error) {
        output.innerHTML = '<p class="sidebar-empty">' + escapeHtml(error.message) + "</p>";
      });
  }

  // -------------------------------------------------------------- stellar

  var stellarLoaded = false;
  var tools = window.StellarTools;

  function openStellarFile(path) {
    loadFile(path);
  }

  function loadStellarDetection() {
    var output = document.getElementById("stellar-detection-output");
    if (!output) return;
    output.innerHTML = '<p class="sidebar-empty">Loading Stellar/Soroban detection…</p>';
    tools
      .apiGet("/workspaces/api/projects/" + PROJECT_ID + "/stellar")
      .then(function (data) {
        output.innerHTML = tools.renderDetection(data);
        tools.bindFileLinks(output);
      })
      .catch(function (error) {
        output.innerHTML = tools.errorMessage(error);
      });
  }

  function loadStellarNetwork() {
    var output = document.getElementById("stellar-network-output");
    if (!output) return;
    output.innerHTML = '<p class="sidebar-empty">Loading network status…</p>';
    tools
      .apiGet("/stellar/api/network")
      .then(function (data) {
        output.innerHTML = tools.renderNetworkStatus(data);
      })
      .catch(function (error) {
        output.innerHTML = tools.errorMessage(error);
      });
  }

  function loadStellar() {
    if (stellarLoaded) return;
    stellarLoaded = true;
    loadStellarDetection();
    loadStellarNetwork();
  }

  function stellarLookup(kind) {
    var outputId = kind === "ledger-entry" ? "stellar-ledger-output" : "stellar-" + kind + "-output";
    var output = document.getElementById(outputId);
    if (!output) return;
    var url = null;
    if (kind === "account") {
      var address = document.getElementById("stellar-account-input").value.trim();
      if (!address) {
        output.innerHTML = tools.errorMessage({ message: "Enter a G… account address." });
        return;
      }
      url = "/stellar/api/account?address=" + encodeURIComponent(address);
    } else if (kind === "contract") {
      var contractId = document.getElementById("stellar-contract-input").value.trim();
      var wasmHash = document.getElementById("stellar-wasm-input").value.trim();
      if (!contractId) {
        output.innerHTML = tools.errorMessage({ message: "Enter a C… contract id." });
        return;
      }
      url = "/stellar/api/contract?address=" + encodeURIComponent(contractId);
      if (wasmHash) url += "&wasm_hash=" + encodeURIComponent(wasmHash);
    } else if (kind === "ledger-entry") {
      var key = document.getElementById("stellar-ledger-input").value.trim();
      if (!key) {
        output.innerHTML = tools.errorMessage({ message: "Enter a base64 ledger key." });
        return;
      }
      url = "/stellar/api/ledger-entry?key=" + encodeURIComponent(key);
    }
    if (!url) return;
    output.innerHTML = tools.loading();
    tools
      .apiGet(url)
      .then(function (data) {
        if (kind === "account") {
          output.innerHTML = tools.renderAccount(data);
        } else if (kind === "contract") {
          output.innerHTML = tools.renderContract(data);
        } else {
          output.innerHTML = tools.renderLedgerEntry(data);
        }
      })
      .catch(function (error) {
        output.innerHTML = tools.errorMessage(error);
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

  // ------------------------------------------------- inline review comments

  function countCodeBlocks(content) {
    var matches = (content || "").match(/```/g);
    return matches ? Math.floor(matches.length / 2) : 0;
  }

  function anchorText(comment) {
    if (comment.block_index == null && comment.line_start == null) return "whole message";
    var parts = [];
    if (comment.block_index != null) parts.push("code block " + (comment.block_index + 1));
    if (comment.line_start != null) {
      parts.push(
        "line " + comment.line_start +
        (comment.line_end && comment.line_end !== comment.line_start ? "-" + comment.line_end : "")
      );
    }
    return parts.join(" · ");
  }

  function reviewCommentUrl(messageId, commentId) {
    var base =
      "/workspaces/api/projects/" + PROJECT_ID + "/messages/" + messageId + "/review-comments";
    return commentId == null ? base : base + "/" + commentId;
  }

  function metricPill(value, label) {
    return (
      '<div class="metric-card"><span class="metric-value">' + value +
      '</span><span class="metric-label">' + label + "</span></div>"
    );
  }

  function threadHtml(thread) {
    var resolved = thread.resolved;
    var html =
      '<div class="review-thread' + (resolved ? " review-thread-resolved" : "") +
      '" data-comment-id="' + thread.id + '">' +
      '<div class="review-thread-head">' +
      '<span class="review-anchor">' + escapeHtml(anchorText(thread)) + "</span>" +
      '<span class="tag ' + (resolved ? "tag-confirmed" : "tag-suggestion") + '">' +
      (resolved ? "resolved" : "open") + "</span>" +
      '<button class="btn btn-ghost btn-sm review-resolve" type="button" data-resolved="' +
      (resolved ? "0" : "1") + '">' + (resolved ? "Reopen" : "Resolve") + "</button>" +
      '<button class="btn btn-ghost btn-sm review-delete" type="button">Delete</button>' +
      "</div>" +
      '<div class="review-comment">' + renderInline(thread.body) +
      ' <span class="review-author">— ' + escapeHtml(thread.author_username || "unknown") +
      "</span></div>";
    (thread.replies || []).forEach(function (reply) {
      html +=
        '<div class="review-comment review-reply" data-comment-id="' + reply.id + '">' +
        renderInline(reply.body) +
        ' <span class="review-author">— ' + escapeHtml(reply.author_username || "unknown") +
        '</span> <button class="btn btn-ghost btn-sm review-delete" type="button">Delete</button>' +
        "</div>";
    });
    html +=
      '<div class="review-reply-box">' +
      '<input class="review-reply-input" type="text" maxlength="4000" placeholder="Reply...">' +
      '<button class="btn btn-ghost btn-sm review-reply-btn" type="button">Reply</button>' +
      "</div></div>";
    return html;
  }

  function renderMessageReview(messageEl, messageId) {
    var panel = messageEl.querySelector(".message-review-panel");
    if (!panel) return;
    var content = messageEl.__reviewContent || "";
    api(reviewCommentUrl(messageId))
      .then(function (data) {
        var threads = data.items || [];
        var html = "";
        if (!threads.length) {
          html += '<p class="sidebar-empty">No inline comments yet.</p>';
        } else {
          threads.forEach(function (thread) {
            html += threadHtml(thread);
          });
        }
        var blockCount = countCodeBlocks(content);
        html +=
          '<div class="review-composer">' +
          '<textarea class="review-composer-input" rows="2" maxlength="4000" ' +
          'placeholder="Add a review comment... @username to mention"></textarea>' +
          '<div class="review-composer-controls">' +
          '<select class="review-block-select"><option value="">Whole message</option>';
        for (var i = 0; i < blockCount; i++) {
          html += '<option value="' + i + '">Code block ' + (i + 1) + "</option>";
        }
        html +=
          "</select>" +
          '<input class="review-line-start" type="number" min="1" placeholder="start line">' +
          '<input class="review-line-end" type="number" min="1" placeholder="end line">' +
          '<button class="btn btn-primary btn-sm review-composer-btn" type="button">Comment</button>' +
          "</div></div>";
        panel.innerHTML = html;
      })
      .catch(function (error) {
        panel.innerHTML = '<p class="sidebar-empty">' + escapeHtml(error.message) + "</p>";
      });
  }

  function loadReviewSummary() {
    if (!reviewSummaryEl) return;
    api("/workspaces/api/projects/" + PROJECT_ID + "/review-summary")
      .then(function (summary) {
        if (!summary.total) {
          reviewSummaryEl.innerHTML =
            '<h3>Review threads</h3><p class="sidebar-empty">No inline review threads yet. ' +
            "Open an assistant message to add one.</p>";
          return;
        }
        var html = "<h3>Review threads</h3><div class='metric-grid'>" +
          metricPill(summary.open, "open") +
          metricPill(summary.resolved, "resolved") +
          metricPill(summary.total, "total") +
          "</div>";
        summary.threads.forEach(function (thread) {
          html +=
            '<div class="review-summary-row">' +
            '<span class="tag ' + (thread.resolved ? "tag-confirmed" : "tag-suggestion") + '">' +
            (thread.resolved ? "resolved" : "open") + "</span> " +
            '<a href="#" class="review-summary-jump" data-message-id="' + thread.message_id + '">' +
            escapeHtml(anchorText(thread)) + "</a> " +
            '<span class="review-author">' + escapeHtml(thread.author_username || "unknown") +
            " &middot; " + (thread.reply_count || 0) +
            " repl" + (thread.reply_count === 1 ? "y" : "ies") + "</span></div>";
        });
        reviewSummaryEl.innerHTML = html;
      })
      .catch(function () {
        reviewSummaryEl.innerHTML =
          '<h3>Review threads</h3><p class="sidebar-empty">Review threads unavailable.</p>';
      });
  }

  function reloadMessageReview(messageEl) {
    renderMessageReview(messageEl, messageEl.getAttribute("data-message-id"));
  }

  document.addEventListener("click", function (event) {
    var toggle = event.target.closest(".message-review-toggle");
    if (toggle) {
      var messageEl = toggle.closest(".chat-message");
      var panel = toggle.closest(".message-review").querySelector(".message-review-panel");
      if (panel.hidden) {
        panel.hidden = false;
        renderMessageReview(messageEl, messageEl.getAttribute("data-message-id"));
      } else {
        panel.hidden = true;
      }
      return;
    }

    var composerBtn = event.target.closest(".review-composer-btn");
    if (composerBtn) {
      var panelC = composerBtn.closest(".message-review-panel");
      var msgEl = composerBtn.closest(".chat-message");
      var input = panelC.querySelector(".review-composer-input");
      var body = input.value.trim();
      if (!body) {
        input.focus();
        return;
      }
      var payload = { body: body };
      var blockSel = panelC.querySelector(".review-block-select");
      if (blockSel && blockSel.value !== "") payload.block_index = parseInt(blockSel.value, 10);
      var lineStart = panelC.querySelector(".review-line-start");
      var lineEnd = panelC.querySelector(".review-line-end");
      if (lineStart && lineStart.value) payload.line_start = parseInt(lineStart.value, 10);
      if (lineEnd && lineEnd.value) payload.line_end = parseInt(lineEnd.value, 10);
      composerBtn.disabled = true;
      api(reviewCommentUrl(msgEl.getAttribute("data-message-id")), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(function () {
        reloadMessageReview(msgEl);
        loadReviewSummary();
      }).catch(function (error) {
        flashError(error.message);
        composerBtn.disabled = false;
      });
      return;
    }

    var replyBtn = event.target.closest(".review-reply-btn");
    if (replyBtn) {
      var thread = replyBtn.closest(".review-thread");
      var msgEl2 = replyBtn.closest(".chat-message");
      var replyInput = thread.querySelector(".review-reply-input");
      var replyBody = replyInput.value.trim();
      if (!replyBody) {
        replyInput.focus();
        return;
      }
      replyBtn.disabled = true;
      api(reviewCommentUrl(msgEl2.getAttribute("data-message-id")), {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({
          body: replyBody,
          parent_id: parseInt(thread.getAttribute("data-comment-id"), 10),
        }),
      }).then(function () {
        reloadMessageReview(msgEl2);
        loadReviewSummary();
      }).catch(function (error) {
        flashError(error.message);
        replyBtn.disabled = false;
      });
      return;
    }

    var resolveBtn = event.target.closest(".review-resolve");
    if (resolveBtn) {
      var threadR = resolveBtn.closest(".review-thread");
      var msgEl3 = resolveBtn.closest(".chat-message");
      api(reviewCommentUrl(msgEl3.getAttribute("data-message-id"), threadR.getAttribute("data-comment-id")), {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ resolved: resolveBtn.getAttribute("data-resolved") === "1" }),
      }).then(function () {
        reloadMessageReview(msgEl3);
        loadReviewSummary();
      }).catch(function (error) {
        flashError(error.message);
      });
      return;
    }

    var deleteBtn = event.target.closest(".review-delete");
    if (deleteBtn) {
      var target = deleteBtn.closest("[data-comment-id]");
      var msgEl4 = deleteBtn.closest(".chat-message");
      if (!window.confirm("Delete this review comment?")) return;
      api(reviewCommentUrl(msgEl4.getAttribute("data-message-id"), target.getAttribute("data-comment-id")), {
        method: "DELETE",
      }).then(function () {
        reloadMessageReview(msgEl4);
        loadReviewSummary();
      }).catch(function (error) {
        flashError(error.message);
      });
      return;
    }

    var jump = event.target.closest(".review-summary-jump");
    if (jump) {
      event.preventDefault();
      var targetMsg = chatMessagesEl.querySelector(
        '.chat-message[data-message-id="' + jump.getAttribute("data-message-id") + '"]'
      );
      if (targetMsg) targetMsg.scrollIntoView({ behavior: "smooth", block: "center" });
    }
  });

  // ----------------------------------------------------------------- init

  document.addEventListener("DOMContentLoaded", function () {
    var parts = window.location.pathname.split("/").filter(Boolean);
    PROJECT_ID = parseInt(parts[parts.length - 1], 10) || 0;

    var rootUl = document.createElement("ul");
    rootUl.className = "tree-children";
    treeEl.appendChild(rootUl);
    loadDir("", rootUl);
    renderBreadcrumbs("");

    if (breadcrumbsEl) {
      breadcrumbsEl.addEventListener("click", function (event) {
        var crumb = event.target.closest(".file-crumb");
        if (!crumb) return;
        navigateToDir(crumb.getAttribute("data-path") || "");
      });
    }

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

    if (window.StellarTools) {
      window.StellarTools.onOpenFile = openStellarFile;
    }
    if (document.getElementById("stellar-network-refresh")) {
      document.getElementById("stellar-network-refresh").addEventListener("click", loadStellarNetwork);
    }
    if (document.getElementById("stellar-account-btn")) {
      document.getElementById("stellar-account-btn").addEventListener("click", function () {
        stellarLookup("account");
      });
      document.getElementById("stellar-account-input").addEventListener("keydown", function (event) {
        if (event.key === "Enter") stellarLookup("account");
      });
    }
    if (document.getElementById("stellar-contract-btn")) {
      document.getElementById("stellar-contract-btn").addEventListener("click", function () {
        stellarLookup("contract");
      });
      document.getElementById("stellar-contract-input").addEventListener("keydown", function (event) {
        if (event.key === "Enter") stellarLookup("contract");
      });
      document.getElementById("stellar-wasm-input").addEventListener("keydown", function (event) {
        if (event.key === "Enter") stellarLookup("contract");
      });
    }
    if (document.getElementById("stellar-ledger-btn")) {
      document.getElementById("stellar-ledger-btn").addEventListener("click", function () {
        stellarLookup("ledger-entry");
      });
      document.getElementById("stellar-ledger-input").addEventListener("keydown", function (event) {
        if (event.key === "Enter") stellarLookup("ledger-entry");
      });
    }

    chatSendBtn.addEventListener("click", startChat);
    newChatSessionBtn.addEventListener("click", createChatSession);
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

    // Export (#107): normal navigation download. When the server answers with
    // JSON instead of a zip, surface the error message instead of saving it.
    var exportLink = document.getElementById("export-project");
    if (exportLink) {
      exportLink.addEventListener("click", function (event) {
        var response = null;
        var check = fetch(exportLink.href, { headers: { Accept: "application/zip" } })
          .then(function (res) {
            response = res;
            var type = res.headers.get("Content-Type") || "";
            if (type.indexOf("application/json") === -1) return null;
            return res.json();
          })
          .then(function (data) {
            if (data && data.error) {
              event.preventDefault();
              flashError(data.error);
            }
          })
          .catch(function () {
            /* network hiccup: let the navigation proceed */
          });
        if (check && check.then) event.preventDefault();
      });
    }

    // Refresh (#88): re-import from the original source and swap the file set.
    var refreshBtn = document.getElementById("refresh-project");
    if (refreshBtn) {
      refreshBtn.addEventListener("click", function () {
        if (!confirm("Re-import this project from its source? The stored files will be replaced.")) {
          return;
        }
        refreshBtn.disabled = true;
        refreshBtn.textContent = "Refreshing...";
        api("/workspaces/api/projects/" + PROJECT_ID + "/refresh", { method: "POST" })
          .then(function () {
            window.location.reload();
          })
          .catch(function (error) {
            refreshBtn.disabled = false;
            refreshBtn.textContent = "Refresh";
            flashError(error.message);
          });
      });
    }

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

    // Import flows can deep-link straight into the Stellar tab.
    var params = new URLSearchParams(window.location.search);
    if (params.get("tab") === "stellar" && document.getElementById("tab-stellar")) {
      switchTab("stellar");
    }
  });
})();
