// AI Code Assistant — chat UI
// Conversation list, SSE streaming of assistant replies, client-side
// markdown rendering, and conversation management (rename/pin/delete/export).

(function () {
  "use strict";

  var messagesEl = document.getElementById("chat-messages");
  var inputEl = document.getElementById("chat-input");
  var sendBtn = document.getElementById("send-message");
  var listEl = document.getElementById("conversation-list");
  var searchEl = document.getElementById("conversation-search");
  var actionsEl = document.getElementById("conversation-actions");
  var currentId = null;
  var streaming = false;
  var currentController = null;
  var cancelRequested = false;

  var CSRF_TOKEN = null;

  function getCsrf() {
    if (CSRF_TOKEN !== null) return CSRF_TOKEN;
    var meta = document.querySelector('meta[name="csrf-token"]');
    if (meta) {
      CSRF_TOKEN = meta.content;
      return CSRF_TOKEN;
    }
    var input = document.querySelector('input[name="csrf_token"]');
    CSRF_TOKEN = input ? input.value : "";
    return CSRF_TOKEN;
  }

  function escapeHtml(text) {
    var div = document.createElement("div");
    div.textContent = text;
    return div.innerHTML;
  }

  // Markdown rendering lives in chat_markdown.js, which escapes all text and
  // sanitizes the generated HTML before it can be inserted into the DOM. If
  // that module failed to load we fall back to a plain escaped rendering.
  function renderMarkdown(text) {
    if (window.AICAMarkdown && window.AICAMarkdown.renderMarkdown) {
      return window.AICAMarkdown.renderMarkdown(text);
    }
    return escapeHtml(text).replace(/\n/g, "<br>\n");
  }

  function addMessage(role, content) {
    var el = document.createElement("div");
    el.className = "chat-message chat-" + role;
    var label = role === "user" ? "You" : "Assistant";
    var body =
      '<div class="message-header">' +
      escapeHtml(label) +
      '</div><div class="message-body">' +
      (role === "user" ? escapeHtml(content) : renderMarkdown(content)) +
      "</div>";
    el.innerHTML = body;
    messagesEl.appendChild(el);
    scrollToBottom();
    return el;
  }

  function addTypingIndicator() {
    var el = document.createElement("div");
    el.className = "chat-message chat-assistant typing";
    el.innerHTML = '<div class="message-header">Assistant</div><div class="typing-indicator"><span></span><span></span><span></span></div>';
    messagesEl.appendChild(el);
    scrollToBottom();
    return el;
  }

  function scrollToBottom() {
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  function setActiveItem(id) {
    var items = listEl.querySelectorAll(".conversation-item");
    items.forEach(function (item) {
      item.classList.toggle("active", Number(item.dataset.id) === Number(id));
    });
  }

  function updateTitle(item, title) {
    item.querySelector(".conversation-title").textContent = title;
    item.dataset.title = title;
  }

  async function api(url, options) {
    options = options || {};
    options.headers = Object.assign({}, options.headers || {}, {
      "X-CSRFToken": getCsrf(),
    });
    var response = await fetch(url, options);
    var data;
    try {
      data = await response.json();
    } catch (e) {
      data = null;
    }
    if (!response.ok) {
      var message = data && data.error ? data.error : "Request failed (" + response.status + ").";
      throw new Error(message);
    }
    return data;
  }

  function loadConversation(id) {
    currentId = Number(id);
    setActiveItem(id);
    messagesEl.innerHTML = "";
    actionsEl.hidden = false;
    return api("/chat/conversations/" + id)
      .then(function (data) {
        data.messages.forEach(function (message) {
          addMessage(message.role, message.content);
        });
        if (data.messages.length === 0) {
          messagesEl.innerHTML =
            '<div class="chat-placeholder"><p>Ask the AI assistant for help with your code.</p></div>';
        }
      })
      .catch(function (error) {
        flashError(error.message);
      });
  }

  function newConversation() {
    currentId = null;
    messagesEl.innerHTML = '<div class="chat-placeholder"><p>Start a new conversation.</p></div>';
    actionsEl.hidden = true;
    inputEl.focus();
  }

  // Toggle the composer button between Send (idle) and Stop (while streaming).
  function setComposerState(state) {
    if (state === "streaming") {
      sendBtn.textContent = "Stop";
      sendBtn.classList.add("btn-danger");
      sendBtn.classList.remove("btn-primary");
      sendBtn.disabled = false;
      sendBtn.setAttribute("aria-label", "Stop generating");
    } else if (state === "stopping") {
      sendBtn.textContent = "Stopping...";
      sendBtn.disabled = true;
      sendBtn.setAttribute("aria-label", "Stopping generation");
    } else {
      sendBtn.textContent = "Send";
      sendBtn.classList.remove("btn-danger");
      sendBtn.classList.add("btn-primary");
      sendBtn.disabled = false;
      sendBtn.setAttribute("aria-label", "Send message");
    }
  }

  function stopStream() {
    if (!streaming || !currentController) return;
    // Stay in-flight until the abort resolves so a fast double-click cannot
    // open a second stream (double-submit protection during the cancel window).
    cancelRequested = true;
    setComposerState("stopping");
    currentController.abort();
  }

  async function startStream() {
    var content = inputEl.value.trim();
    if (!content || streaming) return;
    if (currentId === null) {
      try {
        var created = await api("/chat/conversations", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({}),
        });
        addListItem(created);
        currentId = created.id;
        actionsEl.hidden = false;
      } catch (error) {
        flashError(error.message);
        return;
      }
    }

    inputEl.value = "";
    streaming = true;
    cancelRequested = false;
    currentController = new AbortController();
    setComposerState("streaming");
    addMessage("user", content);

    var typing = addTypingIndicator();
    var bodyEl = typing.querySelector(".typing-indicator");

    try {
      var response = await fetch("/chat/conversations/" + currentId + "/stream", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": getCsrf(),
        },
        body: JSON.stringify({ content: content }),
        signal: currentController.signal,
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
      streamBody.textContent = "";

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
            scrollToBottom();
          } else if (payload.type === "error") {
            flashError(payload.error);
          } else if (payload.type === "done") {
            if (payload.message) {
              streamBody.innerHTML = renderMarkdown(payload.message.content);
            }
            scrollToBottom();
          }
        });
      }
    } catch (error) {
      if (error && error.name === "AbortError") {
        // The user pressed Stop: keep the partial reply already on screen.
        flashInfo(cancelRequested ? "Generation stopped." : "Stream aborted.");
      } else {
        flashError(error.message);
      }
    } finally {
      typing.classList.remove("typing", "streaming");
      if (!typing.querySelector(".message-body") || !typing.querySelector(".message-body").textContent) {
        typing.remove();
      }
      currentController = null;
      cancelRequested = false;
      streaming = false;
      setComposerState("idle");
    }
  }

  function addListItem(conversation) {
    var li = document.createElement("li");
    li.className = "conversation-item";
    li.dataset.id = conversation.id;
    li.dataset.title = conversation.title;
    li.innerHTML =
      '<span class="conversation-title">' +
      escapeHtml(conversation.title) +
      '</span><span class="conversation-meta">0 messages</span>';
    listEl.appendChild(li);
  }

  function refreshList() {
    var query = searchEl ? searchEl.value.trim() : "";
    api("/chat/conversations" + (query ? "?q=" + encodeURIComponent(query) : ""))
      .then(function (items) {
        listEl.innerHTML = "";
        items.forEach(addListItem);
        if (items.length === 0) {
          listEl.innerHTML = '<p class="sidebar-empty">No conversations match your search.</p>';
        }
      })
      .catch(function (error) {
        flashError(error.message);
      });
  }

  function flashError(message) {
    var el = document.createElement("div");
    el.className = "flash flash-error";
    el.textContent = message;
    messagesEl.prepend(el);
  }

  function flashInfo(message) {
    var el = document.createElement("div");
    el.className = "flash flash-info";
    el.textContent = message;
    messagesEl.prepend(el);
  }

  document.addEventListener("DOMContentLoaded", function () {
    listEl.addEventListener("click", function (event) {
      var item = event.target.closest(".conversation-item");
      if (item && !streaming) loadConversation(item.dataset.id);
    });

    sendBtn.addEventListener("click", function () {
      if (streaming) {
        stopStream();
      } else {
        startStream();
      }
    });
    inputEl.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        startStream();
      }
    });

    document.getElementById("new-conversation").addEventListener("click", newConversation);

    var params = new URLSearchParams(window.location.search);
    var openId = params.get("conversation");
    if (openId) {
      loadConversation(openId);
    }

    if (searchEl) {
      searchEl.addEventListener("input", function () {
        refreshList();
      });
    }

    actionsEl.addEventListener("click", function (event) {
      var id = currentId;
      var button = event.target.closest("button");
      if (!button || id === null) return;
      if (button.id === "rename-conversation") {
        var title = prompt("Rename conversation:", "");
        if (title === null) return;
        api("/chat/conversations/" + id, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ title: title }),
        }).then(function (conversation) {
          var item = listEl.querySelector('.conversation-item[data-id="' + id + '"]');
          if (item) updateTitle(item, conversation.title);
        }).catch(function (error) {
          flashError(error.message);
        });
      } else if (button.id === "pin-conversation") {
        api("/chat/conversations/" + id, {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ is_pinned: true }),
        }).then(function () {
          refreshList();
        }).catch(function (error) {
          flashError(error.message);
        });
      } else if (button.id === "share-conversation") {
        var username = prompt("Share this conversation with username:", "");
        if (username === null) return;
        api("/chat/conversations/" + id + "/shares", {
          method: "POST",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ username: username.trim() }),
        }).then(function () {
          var note = document.createElement("div");
          note.className = "flash flash-success";
          note.textContent = "Conversation shared.";
          messagesEl.prepend(note);
        }).catch(function (error) {
          flashError(error.message);
        });
      } else if (button.id === "export-conversation") {
        window.location.href = "/chat/conversations/" + id + "/export";
      } else if (button.id === "delete-conversation") {
        if (!confirm("Delete this conversation?")) return;
        api("/chat/conversations/" + id, { method: "DELETE" }).then(function () {
          newConversation();
          refreshList();
        }).catch(function (error) {
          flashError(error.message);
        });
      }
    });
  });
})();
