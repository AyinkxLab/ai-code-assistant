// AI Code Assistant — chat UI
// Conversation list, SSE streaming of assistant replies, client-side
// markdown rendering, and conversation management (rename/pin/delete/export).

(function () {
  "use strict";

  var messagesEl = document.getElementById("chat-messages");
  var inputEl = document.getElementById("chat-input");
  var sendBtn = document.getElementById("send-message");
  var composerErrorEl = document.getElementById("composer-error");
  var usageEl = document.getElementById("conversation-usage");
  var conversationUsage = { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 };
  var listEl = document.getElementById("conversation-list");
  var searchEl = document.getElementById("conversation-search");
  var actionsEl = document.getElementById("conversation-actions");
  var currentId = null;
  var streaming = false;
  var currentController = null;
  var cancelRequested = false;
  var onboardingEl = document.getElementById("provider-onboarding");
  var imageInput = document.getElementById("chat-image-input");
  var attachBtn = document.getElementById("chat-attach-image");
  var previewEl = document.getElementById("chat-image-preview");
  var pendingAttachments = [];
  var MAX_ATTACHMENTS = 4;
  var providerEl = document.getElementById("chat-provider");
  var modelEl = document.getElementById("chat-model");
  var tempEl = document.getElementById("chat-temperature");
  var tempValueEl = document.getElementById("chat-temperature-value");
  var systemEl = document.getElementById("chat-system-prompt");
  var providerOptions = [];
  var defaults = { provider: "", model: "", temperature: 0.7, system_prompt: "" };

  // Streaming scroll safety (issue #9): auto-follow new tokens only while the
  // user is already pinned to the bottom, so scrolling up mid-stream is never
  // yanked back down.
  var stickToBottom = true;
  var SCROLL_STICK_THRESHOLD_PX = 40;

  // Retry/backoff configuration for transient provider failures.
  var MAX_RETRIES = 3;
  var BASE_BACKOFF_MS = 500;
  var MAX_BACKOFF_MS = 8000;
  var CIRCUIT_FAILURE_THRESHOLD = 3;
  var CIRCUIT_COOLDOWN_MS = 30000;

  var providerFailures = {};

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

  function formatRelativeTime(iso) {
    if (!iso) return "";
    var then = new Date(iso);
    if (isNaN(then.getTime())) return "";
    var seconds = Math.round((then.getTime() - Date.now()) / 1000);
    var relative = typeof Intl !== "undefined" && Intl.RelativeTimeFormat
      ? new Intl.RelativeTimeFormat(undefined, { numeric: "auto" })
      : null;
    var units = [
      ["year", 31536000], ["month", 2592000], ["week", 604800],
      ["day", 86400], ["hour", 3600], ["minute", 60],
    ];
    for (var i = 0; i < units.length; i++) {
      var unit = units[i][0];
      var secs = units[i][1];
      if (Math.abs(seconds) >= secs || unit === "minute") {
        var value = Math.round(seconds / secs);
        if (relative) return relative.format(value, unit);
        return Math.abs(value) + " " + unit + (Math.abs(value) === 1 ? "" : "s") + " ago";
      }
    }
    return relative ? relative.format(seconds, "second") : "just now";
  }

  function updateTimestamps(root) {
    (root || document).querySelectorAll(".conversation-time").forEach(function (el) {
      var iso = el.getAttribute("datetime");
      if (iso) el.textContent = formatRelativeTime(iso);
    });
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

  function renderAttachments(attachments) {
    if (!attachments || !attachments.length) return "";
    return attachments
      .map(function (attachment) {
        return (
          '<img class="chat-attachment-image" src="' +
          escapeHtml(attachment.url) +
          '" alt="' +
          escapeHtml(attachment.filename || "attachment") +
          '" loading="lazy">'
        );
      })
      .join("");
  }

  // Render the running conversation total (issue #13).
  function renderUsage() {
    if (!usageEl) return;
    var total = conversationUsage && conversationUsage.total_tokens;
    usageEl.textContent = total
      ? "This conversation: " +
        total.toLocaleString() +
        " tokens (" +
        (conversationUsage.prompt_tokens || 0).toLocaleString() +
        " prompt / " +
        (conversationUsage.completion_tokens || 0).toLocaleString() +
        " completion)"
      : "";
  }

  function usageLine(usage) {
    if (!usage || !usage.total_tokens) return "";
    return (
      '<div class="message-usage">' +
      usage.total_tokens.toLocaleString() +
      " tokens (" +
      (usage.prompt_tokens || 0).toLocaleString() +
      " + " +
      (usage.completion_tokens || 0).toLocaleString() +
      ")</div>"
    );
  }

  // Render an error banner inside a message element so failed assistant
  // replies carry a visible error state in the UI (acceptance criteria).
  function errorBanner(message, retryable) {
    var cls = "message-error" + (retryable ? " retryable" : "");
    return (
      '<div class="' +
      cls +
      '" role="alert"><span class="message-error-icon" aria-hidden="true">⚠</span><span class="message-error-text">' +
      escapeHtml(message) +
      "</span></div>"
    );
  }

  function addMessage(role, content, attachments, usage, error) {
    var el = document.createElement("div");
    el.className = "chat-message chat-" + role;
    if (error && error.status === "error") el.className += " error";
    var label = role === "user" ? "You" : "Assistant";
    var body =
      '<div class="message-header">' +
      escapeHtml(label) +
      '</div><span class="message-body">' +
      (role === "user" ? escapeHtml(content) : renderMarkdown(content)) +
      "</span>" +
      renderAttachments(attachments) +
      (role === "user" ? "" : usageLine(usage)) +
      (error && error.status === "error"
        ? errorBanner(error.message || "Request failed.", error.retryable)
        : "");
    el.innerHTML = body;
    messagesEl.appendChild(el);
    if (role !== "user") enhanceCode(el);
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

  function isNearBottom() {
    if (!messagesEl) return true;
    return (
      messagesEl.scrollHeight - messagesEl.scrollTop - messagesEl.clientHeight <
      SCROLL_STICK_THRESHOLD_PX
    );
  }

  // Force-follow to the newest content. Used when the user sends a message or
  // opens a conversation, where jumping to the bottom is the expected behavior.
  function scrollToBottom() {
    stickToBottom = true;
    messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  // Follow new content only while the user is already pinned to the bottom, so
  // a streaming token update never resets their scroll position (issue #9).
  function maybeScrollToBottom() {
    if (stickToBottom) messagesEl.scrollTop = messagesEl.scrollHeight;
  }

  if (messagesEl) {
    messagesEl.addEventListener("scroll", function () {
      stickToBottom = isNearBottom();
    });
  }

  // Copy-to-clipboard helpers for code blocks (issue #9).
  function copyTextToClipboard(text) {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      navigator.clipboard.writeText(text).then(
        function () {
          flashInfo("Copied to clipboard.");
        },
        function () {
          fallbackCopyText(text);
        }
      );
      return;
    }
    fallbackCopyText(text);
  }

  function fallbackCopyText(text) {
    var area = document.createElement("textarea");
    area.value = text;
    area.setAttribute("readonly", "");
    area.style.position = "absolute";
    area.style.left = "-10000px";
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
      flashInfo("Copied to clipboard.");
    } catch (err) {
      flashError("Could not copy to clipboard.");
    }
    document.body.removeChild(area);
  }

  function enhanceCode(root) {
    (root || document).querySelectorAll("pre code").forEach(function (codeEl) {
      var pre = codeEl.parentElement;
      if (!pre || pre.querySelector(".code-copy-btn")) return;
      var btn = document.createElement("button");
      btn.type = "button";
      btn.className = "code-copy-btn";
      btn.textContent = "Copy";
      btn.addEventListener("click", function () {
        copyTextToClipboard(codeEl.innerText);
      });
      pre.appendChild(btn);
    });
  }

  function flashInfo(message) {
    if (!composerErrorEl) return;
    composerErrorEl.textContent = message;
    composerErrorEl.className = "composer-notice";
    window.setTimeout(function () {
      if (composerErrorEl.textContent === message) composerErrorEl.textContent = "";
    }, 2500);
  }

  function flashError(message) {
    if (!composerErrorEl) return;
    composerErrorEl.textContent = message;
    composerErrorEl.className = "composer-error";
  }

  function clearError() {
    if (!composerErrorEl) return;
    composerErrorEl.textContent = "";
    composerErrorEl.className = "";
  }

  function autoResize() {
    if (!inputEl) return;
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + "px";
  }

  function setStreaming(value) {
    streaming = value;
    if (sendBtn) sendBtn.disabled = value;
  }

  function updateSendButton() {
    if (!sendBtn) return;
    sendBtn.disabled = streaming;
  }

  function sendMessage() {
    if (streaming) return;
    var text = inputEl ? inputEl.value.trim() : "";
    if (!text && !pendingAttachments.length) return;
    clearError();
    addMessage("user", text, pendingAttachments.slice());
    var attachments = pendingAttachments.slice();
    pendingAttachments = [];
    renderPendingAttachments();
    if (inputEl) inputEl.value = "";
    autoResize();
    streamAssistantReply(text, attachments);
  }

  function renderPendingAttachments() {
    if (!previewEl) return;
    previewEl.innerHTML = "";
    pendingAttachments.forEach(function (attachment, index) {
      var wrap = document.createElement("div");
      wrap.className = "chat-attachment-preview-item";
      var img = document.createElement("img");
      img.src = attachment.url;
      img.alt = attachment.filename || "attachment";
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "chat-attachment-remove";
      remove.textContent = "x";
      remove.addEventListener("click", function () {
        pendingAttachments.splice(index, 1);
        renderPendingAttachments();
      });
      wrap.appendChild(img);
      wrap.appendChild(remove);
      previewEl.appendChild(wrap);
    });
  }

  function uploadImage(file) {
    if (!file) return;
    if (pendingAttachments.length >= MAX_ATACHMENTS) {
      flashError("Attach up to " + MAX_ATACHMENTS + " images.");
      return;
    }
    var form = new FormData();
    form.append("file", file);
    fetch("/api/uploads", {
      method: "POST",
      headers: { "X-CSRF-Token": getCsrf() },
      body: form,
    })
      .then(function (response) {
        if (!response.ok) throw new Error("Upload failed");
        return response.json();
      })
      .then(function (data) {
        pendingAttachments.push({
          url: data.url,
          filename: data.filename || file.name,
        });
        renderPendingAttachments();
      })
      .catch(function () {
        flashError("Could not upload image.");
      });
  }

  function streamAssistantReply(text, attachments) {
    setStreaming(true);
    cancelRequested = false;
    currentController = new AbortController();
    var typing = addTypingIndicator();
    var assistantEl = null;
    var accumulated = "";
    var usage = null;

    var payload = {
      conversation_id: currentId,
      message: text,
      attachments: attachments || [],
      provider: providerEl ? providerEl.value : defaults.provider,
      model: modelEl ? modelEl.value : defaults.model,
      temperature: tempEl ? parseFloat(temEl.value) : defaults.temperature,
      system_prompt: systemEl ? systemEl.value : defaults.system_prompt,
    };

    fetch("/api/chat/stream", {
      method: "POST",
      headers: {
        "Content-Type": "application/json",
        "X-CSRF-Token": getCsrf(),
      },
      body: JSON.stringify(payload),
      signal: currentController.signal,
    })
      .then(function (response) {
        if (!response.ok) {
          return response.json().then(function (data) {
            throw { data: data, status: response.status };
          });
        }
        return readEventStream(response, function (event) {
          if (event.type === "token") {
            if (!assistantEl) {
              if (typing && typing.parentNode) typing.parentNode.removeChild(typing);
              assistantEl = addMessage("assistant", "");
            }
            accumulated += event.data.delta || "";
            var body = assistantEl.querySelector(".message-body");
            if (body) body.innerHTML = renderMarkdown(accumulated);
            maybeScrollToBottom();
          } else if (event.type === "usage") {
            usage = event.data;
          } else if (event.type === "done") {
            if (event.data && event.data.conversation_id) {
              currentId = event.data.conversation_id;
            }
            if (event.data && event.data.usage) {
              usage = event.data.usage;
            }
          } else if (event.type === "error") {
            throw { data: event.data };
          }
        });
      })
      .then(function () {
        if (typing && typing.parentNode) typing.parentNode.removeChild(typing);
        if (usage) {
          conversationUsage = usage;
          renderUsage();
        }
        if (assistantEl && usage) {
          var usageHtml = usageLine(usage);
          if (usageHtml) assistantEl.insertAdjacentHTML("beforeend", usageHtml);
        }
        if (!assistantEl) {
          assistantEl = addMessage("assistant", "");
        }
        enhanceCode(assistantEl);
      })
      .catch(function (result) {
        if (typing && typing.parentNode) typing.parentNode.removeChild(typing);
        if (cancelRequested) return;
        var data = result && result.data ? result.data : {};
        var message = data.message || data.error || "Request failed.";
        var retryable = !!data.retryable;
        if (assistantEl) {
          assistantEl.classList.add("error");
          assistantEl.insertAdjacentHTML("beforeend", errorBanner(message, retryable));
        } else {
          addMessage("assistant", "", null, null, {
            status: "error",
            message: message,
            retryable: retryable,
          });
        }
        flashError(message);
      })
      .finally(function () {
        setStreaming(false);
        currentController = null;
      });
  }

  function readEventStream(response, onEvent) {
    var reader = response.body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";
    function pump() {
      return reader.read().then(function (result) {
        if (result.done) {
          if (buffer.trim()) processBlock(buffer);
          return;
        }
        buffer += decoder.decode(result.value, { stream: true });
        var parts;
        while ((parts = buffer.split("\n\n")).length > 1) {
          var block = parts.shift();
          buffer = parts.join("\n\n");
          processBlock(block);
        }
        return pump();
      });
    }
    function processBlock(block) {
      var lines = block.split("\n");
      var eventType = "message";
      var dataLines = [];
      lines.forEach(function (line) {
        if (line.indexOf("event:") === 0) eventType = line.slice(6).trim();
        else if (line.indexOf("data:") === 0) dataLines.push(line.slice(5).trim());
      });
      if (!dataLines.length) return;
      var data;
      try {
        data = JSON.parse(dataLines.join("\n"));
      } catch (err) {
        return;
      }
      onEvent({ type: eventType, data: data });
    }
    return pump();
  }

  function cancelStream() {
    if (!streaming || !currentController) return;
    cancelRequested = true;
    currentController.abort();
    setStreaming(false);
  }

  // --- Conversation management ---

  function loadConversations() {
    if (!listEl) return;
    var q = searchEl ? searchEl.value.trim() : "";
    var url = "/api/conversations" + (q ? "?q=" + encodeURIComponent(q) : "");
    fetch(url)
      .then(function (response) { return response.json(); })
      .then(function (data) {
        renderConversations(data.conversations || data || []);
      })
      .catch(function () {});
  }

  function renderConversations(conversations) {
    if (!listEl) return;
    listEl.innerHTML = "";
    conversations.forEach(function (conv) {
      var item = document.createElement("li");
      item.className = "conversation-item";
      if (convversation_id === currentId) item.className += " active";
      var title = document.createElement("button");
      title.type = "button";
      title.className = "conversation-title";
      title.textContent = conv.title || "Untitled";
      title.addEventListener("click", function () { openConversation(conv.id); });
      item.appendChild(title);
      listEl.appendChild(item);
    });
  }

  function openConversation(id) {
    currentId = id;
    fetch("/api/conversations/" + id)
      .then(function (response) { return response.json(); })
      .then(function (data) {
        messagesEl.innerHTML = "";
        (data.messages || []).forEach(function (message) {
          addMessage(message.role, message.content, message.attachments, message.usage, message.error);
        });
        scrollToBottom();
      })
      .catch(function () {});
  }

  // --- Event binding ---

  if (sendBtn) sendBtn.addEventListener("click", sendMessage);
  if (inputEl) {
    inputEl.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });
    inputEl.addEventListener("input", autoResize);
  }
  if (attachBtn && imageInput) {
    attachBtn.addEventListener("click", function () { imageInput.click(); });
    imageInput.addEventListener("change", function () {
      if (imageInput.files && imageInput.files[0]) {
        uploadImage(imageInput.files[0]);
        imageInput.value = "";
      }
    });
  }
  if (searchEl) {
    searchEl.addEventListener("input", function () {
      window.clearTimeout(searchEl.__timer);
      searchEl.__timer = window.setTimeout(loadConversations, 250);
    });
  }

  autoResize();
  updateSendButton();
  renderUsage();
  updateTimestamps();
  loadConversations();
})();
