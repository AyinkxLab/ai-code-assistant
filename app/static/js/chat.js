// AI Code Assistant — chat UI
// Conversation list, SSE streaming of assistant replies, client-side
// markdown rendering, and conversation management (rename/pin/delete/export).

(function () {
  "use strict";

  // Expose internals for testing when running under a JS test runner.
  var AICA = (window.AICA = window.AICA || {});

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
  var MAX_ATACHMENTS = 4;
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
    return escapeHtml(text).replace(/\n/g, "<br />\n");
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

  function addMessage(role, content, attachments, usage) {
    var el = document.createElement("div");
    el.className = "chat-message chat-" + role;
    var label = role === "user" ? "You" : "Assistant";
    var body =
      '<div class="message-header">' +
      escapeHtml(label) +
      '</div><div class="message-body">' +
      (role === "user" ? escapeHtml(content) : renderMarkdown(content)) +
      "</div>" +
      renderAttachments(attachments) +
      (role === "user" ? "" : usageLine(usage));
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
    area.style.left = "-9999px";
    document.body.appendChild(area);
    area.select();
    try {
      document.execCommand("copy");
      flashInfo("Copied to clipboard.");
    } catch (error) {
      flashError("Copy failed.");
    }
    document.body.removeChild(area);
  }

  // Add a copy button to every rendered code block, skipping ones that already
  // have one so repeated calls during streaming stay cheap.
  function addCodeCopyButtons(container) {
    if (!container || !container.querySelectorAll) return;
    var blocks = container.querySelectorAll("pre.code-block");
    Array.prototype.forEach.call(blocks, function (pre) {
      if (pre.getElementsByClassName("code-copy-btn").length) return;
      var button = document.createElement("button");
      button.type = "button";
      button.className = "code-copy-btn";
      button.textContent = "Copy";
      button.setAttribute("aria-label", "Copy code to clipboard");
      pre.appendChild(button);
    });
  }

  function enhanceCode(container) {
    highlightCode(container);
    addCodeCopyButtons(container);
  }

  if (messagesEl) {
    messagesEl.addEventListener("click", function (event) {
      var target = event.target;
      var button = target && target.closest ? target.closest(".code-copy-btn") : null;
      if (!button) return;
      var pre = button.closest("pre");
      var code = pre ? pre.querySelector("code") : null;
      if (!code) return;
      copyTextToClipboard(code.innerText || code.textContent || "");
    });
  }

  function setActiveItem(id) {
    var items = listEl.querySelectorAll(".conversation-item");
    items.forEach(function (item) {
      var active = Number(item.dataset.id) === Number(id);
      item.classList.toggle("active", active);
      if (active) item.setAttribute("aria-current", "true");
      else item.removeAttribute("aria-current");
    });
  }

  function updateTitle(item, title) {
    item.querySelector(".conversation-title").textContent = title;
    item.dataset.title = title;
    item.setAttribute("aria-label", "Open conversation: " + title);
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
    loadShareLinks(id);
    return api("/chat/conversations/" + id)
      .then(function (data) {
        data.messages.forEach(function (message) {
          addMessage(message.role, message.content, message.attachments, message.token_usage);
        });
        conversationUsage =
          data.usage || { prompt_tokens: 0, completion_tokens: 0, total_tokens: 0 };
        renderUsage();
        if (data.messages.length === 0) {
          messagesEl.innerHTML =
            '<div class="chat-placeholder"><p>Ask the AI assistant for help with your code.</p></div>';
        }
        applySettingsToPanel(data);
        clearComposerError();
        autoGrowComposer();
        inputEl.focus();
      })
      .catch(function (error) {
        flashError(error.message);
      });
  }

  function loadShareLinks(id) {
    var container = document.getElementById("share-links-list");
    if (!container) return;
    container.innerHTML = "";
    api("/chat/conversations/" + id + "/share-links").then(function (links) {
      if (!links || !links.length) return;
      links.forEach(function (link) {
        var anchor = document.createElement("a");
        anchor.href = link.url;
        anchor.textContent = link.url;
        container.appendChild(anchor);
      });
    }).catch(function () {});
  }

  function applySettingsToPanel(data) {
    if (!data || !data.settings) return;
    var settings = data.settings;
    if (providerEl && settings.provider) providerEl.value = settings.provider;
    if (modelEl && settings.model) modelEl.value = settings.model;
    if (tempEl && typeof settings.temperature === "number") {
      tempEl.value = String(settings.temperature);
      if (tempValueEl) tempValueEl.textContent = String(settings.temperature);
    }
    if (systemEl && typeof settings.system_prompt === "string") {
      systemEl.value = settings.system_prompt;
    }
  }

  function clearComposerError() {
    if (composerErrorEl) composerErrorEl.textContent = "";
  }

  function flashError(message) {
    if (composerErrorEl) composerErrorEl.textContent = message;
  }

  function flashInfo(message) {
    if (composerErrorEl) composerErrorEl.textContent = message;
  }

  function autoGrowComposer() {
    if (!inputEl) return;
    inputEl.style.height = "auto";
    inputEl.style.height = inputEl.scrollHeight + "px";
  }

  function highlightCode() {
    // Optional highlighting hook; no-op when not available.
  }

  function updateSendDisabled() {
    if (!sendBtn || !inputEl) return;
    var hasText = inputEl.value.trim().length > 0;
    sendBtn.disabled = streaming || !hasText;
  }

  function sendMessage() {
    if (streaming) return;
    var text = inputEl ? inputEl.value.trim() : "";
    if ( text) return;
    clearComposerError();
    addMessage("user", text, pendingAttachments);
    if (inputEl) inputEl.value = "";
    pendingAttachments = [];
    if (previewEl) previewEl.innerHTML = "";
    autoGrowComposer();
    updateSendDisabled();
    streaming = true;
    updateSendDisabled();
    var typing = addTypingIndicator();
    var url = currentId ? "/chat/conversations/" + currentId + "/messages" : "/chat/messages";
    currentController = typeof AbortController !== "undefined" ? new AbortController() : null;
    var options = {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ content: text, attachments: pendingAttachments }),
    };
    if (currentController) options.signal = currentController.signal;
    fetch(url, options)
      .then(function (response) {
        if (!response.ok) throw new Error("Request failed.");
        return response.json();
      })
      .then(function (data) {
        if (typing && typing.parentNode) typing.parentNode.removeChild(typing);
        addMessage("assistant", (data && data.content) || "", null, data && data.token_usage);
        if (data && data.conversation_id) {
          currentId = data.conversation_id;
        }
      })
      .catch(function (error) {
        if (typing && typing.parentNode) typing.parentNode.removeChild(typing);
        if (!cancelRequested) flashError(error.message || "Sending failed.");
      })
      .finally(function () {
        streaming = false;
        cancelRequested = false;
        currentController = null;
        updateSendDisabled();
      });
  }

  function cancelStream() {
    if (!currentController) return;
    cancelRequested = true;
    currentController.abort();
  }

  if (inputEl) {
    inputEl.addEventListener("input", function () {
      autoGrowComposer();
      updateSendDisabled();
    });
    inputEl.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        sendMessage();
      }
    });
  }

  if (sendBtn) {
    sendBtn.addEventListener("click", function (event) {
      event.preventDefault();
      sendMessage();
    });
  }

  if (attachBtn && imageInput) {
    attachBtn.addEventListener("click", function () {
      imageInput.click();
    });
  }

  // Expose a minimal testing surface.
  AICA.sendMessage = sendMessage;
  AICA.cancelStream = cancelStream;
  AICA.renderMarkdown = renderMarkdown;
  AICA.addMessage = addMessage;
  AICA.loadConversation = loadConversation;
  AICA.isNearBottom = isNearBottom;
  AICA.updateSendDisabled = updateSendDisabled;
  AICA.getCsrf = getCsrf;
  AICA.formatRelativeTime = formatRelativeTime;
  AICA.setActiveItem = setActiveItem;
})();
