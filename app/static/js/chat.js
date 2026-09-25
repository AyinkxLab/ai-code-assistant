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

  function addMessage(role, content, attachments) {
    var el = document.createElement("div");
    el.className = "chat-message chat-" + role;
    var label = role === "user" ? "You" : "Assistant";
    var body =
      '<div class="message-header">' +
      escapeHtml(label) +
      '</div><div class="message-body">' +
      (role === "user" ? escapeHtml(content) : renderMarkdown(content)) +
      "</div>" +
      renderAttachments(attachments);
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
          addMessage(message.role, message.content, message.attachments);
        });
        if (data.messages.length === 0) {
          messagesEl.innerHTML =
            '<div class="chat-placeholder"><p>Ask the AI assistant for help with your code.</p></div>';
        }
        applySettingsToPanel(data);
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

  async function ensureConversation() {
    if (currentId !== null) return currentId;
    var created = await api("/chat/conversations", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectSettings()),
    });
    addListItem(created);
    currentId = created.id;
    actionsEl.hidden = false;
    return currentId;
  }

  function renderPendingAttachments() {
    if (!previewEl) return;
    previewEl.innerHTML = "";
    pendingAttachments.forEach(function (attachment, index) {
      var chip = document.createElement("span");
      chip.className = "chat-image-chip";
      var img = document.createElement("img");
      img.src = attachment.url;
      img.alt = attachment.filename || "attachment";
      var remove = document.createElement("button");
      remove.type = "button";
      remove.className = "chat-image-remove";
      remove.setAttribute("aria-label", "Remove attached image");
      remove.textContent = "\u00d7";
      remove.addEventListener("click", function () {
        pendingAttachments.splice(index, 1);
        renderPendingAttachments();
      });
      chip.appendChild(img);
      chip.appendChild(remove);
      previewEl.appendChild(chip);
    });
  }

  async function uploadImage(file) {
    if (pendingAttachments.length >= MAX_ATTACHMENTS) {
      flashError("At most " + MAX_ATTACHMENTS + " images can be attached.");
      return;
    }
    try {
      var id = await ensureConversation();
      var form = new FormData();
      form.append("image", file);
      var response = await fetch("/chat/conversations/" + id + "/attachments", {
        method: "POST",
        headers: { "X-CSRFToken": getCsrf() },
        body: form,
      });
      var data = null;
      try {
        data = await response.json();
      } catch (e) {
        data = null;
      }
      if (!response.ok) {
        throw new Error(
          data && data.error ? data.error : "Upload failed (" + response.status + ")."
        );
      }
      pendingAttachments.push(data);
      renderPendingAttachments();
    } catch (error) {
      flashError(error.message);
    }
  }

  async function startStream() {
    var content = inputEl.value.trim();
    if ((!content && pendingAttachments.length === 0) || streaming) return;
    if (onboardingEl && !onboardingEl.hidden) {
      flashError("Add a provider API key before sending a message.");
      onboardingEl.scrollIntoView({ behavior: "smooth", block: "center" });
      return;
    }
    try {
      await ensureConversation();
    } catch (error) {
      flashError(error.message);
      return;
    }

    var attachments = pendingAttachments.slice();
    var attachmentIds = attachments.map(function (attachment) {
      return attachment.id;
    });
    inputEl.value = "";
    pendingAttachments = [];
    renderPendingAttachments();
    streaming = true;
    cancelRequested = false;
    currentController = new AbortController();
    setComposerState("streaming");
    addMessage("user", content || "(image attached)", attachments);

    var typing = addTypingIndicator();
    var bodyEl = typing.querySelector(".typing-indicator");

    try {
      var response = await fetch("/chat/conversations/" + currentId + "/stream", {
        method: "POST",
        headers: {
          "Content-Type": "application/json",
          "X-CSRFToken": getCsrf(),
        },
        body: JSON.stringify({ content: content, attachment_ids: attachmentIds }),
        signal: currentController.signal,
      });

      if (!response.ok) {
        var errData = null;
        try {
          errData = await response.json();
        } catch (e) {
          errData = null;
        }
        if (errData && errData.code === "provider_not_configured") {
          showOnboarding(errData);
          return;
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
              streamBody.innerHTML =
                renderMarkdown(payload.message.content) +
                renderAttachments(payload.message.attachments);
              highlightCode(streamBody);
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
      var finalBody = typing.querySelector(".message-body");
      if (finalBody && finalBody.textContent) {
        // Covers partial output too (e.g. stream cancelled before "done").
        highlightCode(finalBody);
      }
      if (!finalBody || !finalBody.textContent) {
        typing.remove();
      }
      currentController = null;
      cancelRequested = false;
      streaming = false;
      setComposerState("idle");
      sendBtn.disabled = !!(onboardingEl && !onboardingEl.hidden);
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

  // Apply syntax highlighting once markdown has been rendered (issue #44).
  // A no-op when highlight.js is unavailable, and unknown languages are left
  // untouched, so the streaming flow is never interrupted.
  function highlightCode(container) {
    if (window.AICASyntaxHighlight) window.AICASyntaxHighlight.apply(container);
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

  function flashSuccess(message) {
    var el = document.createElement("div");
    el.className = "flash flash-success";
    el.textContent = message;
    messagesEl.prepend(el);
  }

  function showOnboarding(payload) {
    if (!onboardingEl) {
      if (payload && payload.error) flashError(payload.error);
      return;
    }
    onboardingEl.hidden = false;
    if (payload && payload.provider) onboardingEl.dataset.provider = payload.provider;
    var note = document.getElementById("provider-onboarding-note");
    if (note) note.textContent = (payload && payload.error) || "";
    sendBtn.disabled = true;
    onboardingEl.scrollIntoView({ behavior: "smooth", block: "center" });
  }

  function hideOnboarding() {
    if (onboardingEl) onboardingEl.hidden = true;
    sendBtn.disabled = false;
  }

  // Re-check the server's provider status so a key added on another page
  // unlocks the composer without a full reload (issue #25).
  function checkProviderStatus(options) {
    return api("/chat/api/provider-status")
      .then(function (status) {
        if (status && status.configured) {
          var wasVisible = !!(onboardingEl && !onboardingEl.hidden);
          hideOnboarding();
          if (wasVisible && options && options.notify) {
            flashSuccess("Provider key detected — you can send messages now.");
          }
        } else if (onboardingEl) {
          onboardingEl.hidden = false;
          sendBtn.disabled = true;
        }
        return status;
      })
      .catch(function () {
        return null;
      });
  }

  // -- Model & generation settings (issue #12) ----------------------------

  function loadProviderOptions() {
    return api("/chat/api/options")
      .then(function (data) {
        providerOptions = data.providers || [];
        defaults.default_provider = data.default_provider || "";
        defaults.temperature = data.default_temperature != null ? data.default_temperature : 0.7;
      })
      .catch(function (error) {
        flashError(error.message);
      });
  }

  function renderProviderSelect(selected) {
    if (!providerEl) return;
    providerEl.innerHTML = "";
    providerOptions.forEach(function (provider) {
      var option = document.createElement("option");
      option.value = provider.name;
      option.textContent = provider.name + (provider.available ? "" : " (no API key)");
      option.disabled = !provider.available;
      if (!provider.available) {
        option.title = "Add a " + provider.name + " API key on the Keys page to enable it.";
      }
      if (selected === provider.name) option.selected = true;
      providerEl.appendChild(option);
    });
  }

  function renderModelSelect(selected) {
    if (!modelEl) return;
    var provider = providerOptions.filter(function (item) {
      return item.name === providerEl.value;
    })[0];
    var models = provider ? provider.models.slice() : [];
    if (selected && models.indexOf(selected) === -1) models.unshift(selected);
    modelEl.innerHTML = "";
    var automatic = document.createElement("option");
    automatic.value = "";
    automatic.textContent = "Provider default";
    modelEl.appendChild(automatic);
    models.forEach(function (model) {
      var option = document.createElement("option");
      option.value = model;
      option.textContent = model;
      if (selected === model) option.selected = true;
      modelEl.appendChild(option);
    });
  }

  function setTemperature(value) {
    if (!tempEl) return;
    var numeric = isNaN(parseFloat(value)) ? defaults.temperature : parseFloat(value);
    tempEl.value = numeric;
    if (tempValueEl) tempValueEl.textContent = numeric.toFixed(1);
  }

  function firstAvailableProvider() {
    var available = providerOptions.filter(function (item) { return item.available; });
    return available.length ? available[0].name : "";
  }

  function applySettingsToPanel(settings) {
    settings = settings || {};
    var provider = settings.provider || defaults.default_provider || firstAvailableProvider();
    renderProviderSelect(provider);
    if (!providerEl.value || providerEl.selectedOptions[0].disabled) {
      renderProviderSelect(firstAvailableProvider());
    }
    renderModelSelect(settings.model || "");
    setTemperature(settings.temperature != null ? settings.temperature : defaults.temperature);
    if (systemEl) systemEl.value = settings.system_prompt || "";
  }

  function collectSettings() {
    return {
      provider: providerEl ? providerEl.value : "",
      model: modelEl ? modelEl.value : "",
      temperature: tempEl ? parseFloat(tempEl.value) : defaults.temperature,
      system_prompt: systemEl ? systemEl.value.trim() : "",
    };
  }

  function persistSettings() {
    if (currentId === null) return;
    api("/chat/conversations/" + currentId + "/settings", {
      method: "PATCH",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(collectSettings()),
    }).catch(function (error) {
      flashError(error.message);
    });
  }

  var persistTimer = null;
  function persistSettingsDebounced() {
    window.clearTimeout(persistTimer);
    persistTimer = window.setTimeout(persistSettings, 400);
  }


  document.addEventListener("DOMContentLoaded", function () {
    listEl.addEventListener("click", function (event) {
      var item = event.target.closest(".conversation-item");
      if (item && !streaming) loadConversation(item.dataset.id);
    });

    if (onboardingEl && !onboardingEl.hidden) {
      sendBtn.disabled = true;
      var recheck = document.getElementById("provider-recheck");
      if (recheck) {
        recheck.addEventListener("click", function () {
          checkProviderStatus({ notify: true });
        });
      }
      window.addEventListener("focus", function () {
        checkProviderStatus({});
      });
      document.addEventListener("visibilitychange", function () {
        if (!document.hidden) checkProviderStatus({});
      });
    }

    sendBtn.addEventListener("click", function () {
      if (streaming) {
        stopStream();
      } else {
        startStream();
      }
    });

    if (attachBtn && imageInput) {
      attachBtn.addEventListener("click", function () {
        imageInput.click();
      });
      imageInput.addEventListener("change", function () {
        Array.prototype.slice.call(imageInput.files || []).forEach(uploadImage);
        imageInput.value = "";
      });
    }
    inputEl.addEventListener("keydown", function (event) {
      if (event.key === "Enter" && !event.shiftKey) {
        event.preventDefault();
        startStream();
      }
    });

    document.getElementById("new-conversation").addEventListener("click", newConversation);

    if (providerEl) {
      providerEl.addEventListener("change", function () {
        renderModelSelect("");
        persistSettings();
      });
    }
    if (modelEl) {
      modelEl.addEventListener("change", persistSettings);
    }
    if (tempEl) {
      tempEl.addEventListener("input", function () {
        if (tempValueEl) tempValueEl.textContent = parseFloat(tempEl.value).toFixed(1);
      });
      tempEl.addEventListener("change", persistSettings);
    }
    if (systemEl) {
      systemEl.addEventListener("input", persistSettingsDebounced);
    }

    loadProviderOptions().then(function () {
      var params = new URLSearchParams(window.location.search);
      var openId = params.get("conversation");
      if (openId) {
        loadConversation(openId);
      } else {
        applySettingsToPanel({});
      }
    });

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
