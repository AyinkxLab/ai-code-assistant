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
      links.forEach(function (link) {
        var row = document.createElement("p");
        var expiry = link.expires_at ? formatRelativeTime(link.expires_at) : "never";
        row.textContent = link.url + " (expires " + expiry + ")";
        container.appendChild(row);
      });
    });
  }

  // --- Streaming ---

  function applySettingsToPanel(data) {
    if (!data) return;
    if (data.provider) defaults.provider = data.provider;
    if (data.model) defaults.model = data.model;
    if (typeof data.temperature === "number") defaults.temperature = data.temperature;
    if (data.system_prompt) defaults.system_prompt = data.system_prompt;
  }

  function clearComposerError() {
    if (composerErrorEl) {
      composerErrorEl.textContent = "";
      composerErrorEl.hidden = true;
    }
  }

  function flashError(message) {
    if (composerErrorEl) {
      composerErrorEl.textContent = message;
      composerErrorEl.hidden = false;
    }
  }

  function flashInfo(message) {
    if (composerErrorEl) {
      composerErrorEl.textContent = message;
      composerErrorEl.hidden = false;
    }
  }

  function autoGrowComposer() {
    if (!inputEl) return;
    inputEl.style.height = "auto";
    inputEl.style.height = Math.min(inputEl.scrollHeight, 200) + "px";
  }

  function highlightCode(container) {
    if (window.highlightAuto && container) {
      container.querySelectorAll("pre code").forEach(function (block) {
        window.highlightAuto.highlightElement(block);
      });
    }
  }

  function setStreaming(value) {
    streaming = value;
    if (sendBtn) sendBtn.disabled = value;
  }

  function parseSSELine(line) {
    if (!line || line.indexOf("data:") !== 0) return null;
    var payload = line.slice(5).trim();
    if (!payload) return null;
    try {
      return JSON.parse(payload);
    } catch (e) {
      return null;
    }
  }

  async function streamResponse(conversationId, body, assistantEl) {
    currentController = new AbortController();
    cancelRequested = false;
    var response;
    try {
      response = await fetch("/api/conversations/" + conversationId + "/stream", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": getCsrf(),
          Accept: "text/event-stream",
        },
        body: JSON.stringify(body),
        signal: currentController.signal,
      });
    } catch (error) {
      if (error.name === "AbortError") return;
      throw error;
    }

    if (!response.ok) {
      var detail = null;
      try {
        detail = await response.json();
      } catch (e) {
        detail = null;
      }
      throw new Error(
        (detail && detail.error) || ("Stream failed (" + response.status + ").")
      );
    }

    var reader = response.body.getReader();
    var decoder = new TextDecoder();
    var buffer = "";
    var content = "";
    var usage = null;
    var errorMessage = null;

    function handleEvent(event) {
      if (!event) return;
      if (event.type === "content" && typeof event.delta === "string") {
        content += event.delta;
        updateStreamingMessage(assistantEl, content);
      } else if (event.type === "message_end") {
        usage = event.usage || null;
      } else if (event.type === "error") {
        errorMessage = event.message || "Stream error.";
      }
    }

    try {
      while (true) {
        var chunk = await reader.read();
        if (chunk.done) break;
        buffer += decoder.decode(chunk.value, { stream: true });
        var lines = buffer.split("\n");
        buffer = lines.pop();
        for (var i = 0; i < lines.length; i++) {
          handleEvent(parseSSELine(lines[i]));
        }
      }
    } catch (error) {
      if (error.name === "AbortError") {
        // Client disconnected; partial content is persisted server-side.
        return;
      }
      throw error;
    } finally {
      currentController = null;
    }

    if (errorMessage) {
      throw new Error(errorMessage);
    }

    if (usage) {
      conversationUsage = usage;
      renderUsage();
    }
    finallyMessage(assistantEl, content, usage);
  }

  function updateStreamingMessage(el, content) {
    if (!el) return;
    el.classList.remove("typing");
    var body = el.querySelector(".message-body");
    if (body) body.innerHTML = renderMarkdown(content);
    enhanceCode(el);
    maybeScrollToBottom();
  }

  function finallyMessage(el, content, usage) {
    if (!el) return;
    el.classList.remove("typing");
    var body = el.querySelector(".message-body");
    if (body) body.innerHTML = renderMarkdown(content);
    var existing = el.querySelector(".message-usage");
    if (existing) existing.remove();
    if (usage) {
      var wrapper = document.createElement("div");
      wrapper.innerHTML = usageLine(usage);
      el.appendChild(wrapper.firstChild);
    }
    enhanceCode(el);
    maybeScrollToBottom();
  }

  async function sendMessage() {
    if (streaming || !inputEl) return;
    var text = inputEl.value.trim();
    if (!text && !pendingAttachments.length) return;
    clearComposerError();
    var attachments = pendingAttachments.slice();
    pendingAttachments = [];
    renderPendingAttachments();
    inputEl.value = "";
    autoGrowComposer();
    addMessage("user", text, attachments);
    var assistantEl = addTypingIndicator();
    setStreaming(true);
    try {
      var body = {
        message: text,
        attachments: attachments,
        provider: providerEl ? providerEl.value : defaults.provider,
        model: modelEl ? modelEl.value : defaults.model,
        temperature: tempEl ? Number(tempEl.value) : defaults.temperature,
        system_prompt: systemEl ? systemEl.value : defaults.system_prompt,
      };
      await streamResponse(currentId, body, assistantEl);
    } catch (error) {
      if (assistantEl) assistantEl.classList.remove("typing");
      flashError(error.message);
    } finally {
      setStreaming(false);
    }
  }

  function renderPendingAttachments() {
    if (!previewEl) return;
    previewEl.innerHTML = "";
    pendingAttachments.forEach(function (attachment, index) {
      var wrapper = document.createElement("div");
      wrapper.className = "chat-image-preview-item";
      var img = document.createElement("img");
      img.src = attachment.url;
      img.alt = attachment.filename || "attachment";
      var remove = document.createElement("button");
      remove.type = "button";
      remove.textContent = "×";
      remove.setAttribute("aria-label", "Remove attachment");
      remove.addEventListener("click", function () {
        pendingAttachments.splice(index, 1);
        renderPendingAttachments();
      });
      wrapper.appendChild(img);
      wrapper.appendChild(remove);
      previewEl.appendChild(wrapper);
    });
  }

  function handleImageSelection(files) {
    if (!files || !files.length) return;
    Array.prototype.forEach.call(files, function (file) {
      if (pendingAttachments.length >= MAX_ATTACHMENTS) return;
      var reader = new FileReader();
      reader.onload = function () {
        pendingAttachments.push({
          url: reader.result,
          filename: file.name,
        });
        renderPendingAttachments();
      };
      reader.readAsDataURL(file);
    });
  }

  if (attachBtn && imageInput) {
    attachBtn.addEventListener("click", function () {
      imageInput.click();
    });
    imageInput.addEventListener("change", function () {
      handleImageSelection(imageInput.files);
      imageInput.value = "";
    });
  }

  if (inputEl) {
    inputEl.addEventListener("input", autoGrowComposer);
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

  if (tempEl && tempValueEl) {
    tempEl.addEventListener("input", function () {
      tempValueEl.textContent = tempEl.value;
    });
  }

  if (searchEl) {
    searchEl.addEventListener("input", function () {
      var term = searchEl.value.toLowerCase();
      listEl.querySelectorAll(".conversation-item").forEach(function (item) {
        var title = (item.dataset.title || "").toLowerCase();
        item.hidden = term && title.indexOf(term) === -1;
      });
    });
  }

  if (listEl) {
    listEl.addEventListener("click", function (event) {
      var item = event.target.closest(".conversation-item");
      if (!item) return;
      loadConversation(item.dataset.id);
    });
  }

  updateTimestamps();
  setInterval(updateTimestamps, 60000);
})();
