// AI Code Assistant — GitHub integration shared helpers
// Reused by all GitHub pages: CSRF-aware API client, escaping, and rendering.

(function () {
  "use strict";

  window.GitHub = {
    CSRF_TOKEN: null,

    getCsrf: function () {
      if (this.CSRF_TOKEN !== null) return this.CSRF_TOKEN;
      var meta = document.querySelector('meta[name="csrf-token"]');
      if (meta) {
        this.CSRF_TOKEN = meta.content;
        return this.CSRF_TOKEN;
      }
      var input = document.querySelector('input[name="csrf_token"]');
      this.CSRF_TOKEN = input ? input.value : "";
      return this.CSRF_TOKEN;
    },

    api: function (url, options) {
      options = options || {};
      options.headers = Object.assign({}, options.headers || {}, {
        "X-CSRFToken": this.getCsrf(),
      });
      return fetch(url, options).then(function (response) {
        return response.json().then(function (data) {
          if (!response.ok) {
            var error = new Error(data && data.error ? data.error : "Request failed (" + response.status + ").");
            error.kind = data && data.kind;
            if (error.kind === "auth" || error.kind === "not_connected") {
              window.location.assign("/github/connect");
            }
            throw error;
          }
          return data;
        });
      });
    },

    escapeHtml: function (text) {
      var div = document.createElement("div");
      div.textContent = text == null ? "" : String(text);
      return div.innerHTML;
    },

    relativeDate: function (iso) {
      if (!iso) return "";
      var date = new Date(iso);
      if (isNaN(date.getTime())) return iso;
      return date.toLocaleString(undefined, {
        year: "numeric", month: "short", day: "numeric", hour: "2-digit", minute: "2-digit",
      });
    },

    // Escape HTML, then treat newlines as <br> and URLs as links.
    renderMarkdownish: function (text) {
      if (!text) return "";
      var html = this.escapeHtml(text);
      html = html.replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
      html = html.replace(/\r?\n/g, "<br>");
      return html;
    },

    flashError: function (message) {
      var stack = document.querySelector(".flash-stack");
      if (!stack) {
        stack = document.createElement("div");
        stack.className = "flash-stack";
        var main = document.querySelector(".main-content");
        if (main) main.prepend(stack);
        else document.body.prepend(stack);
      }
      var el = document.createElement("div");
      el.className = "flash flash-error";
      el.textContent = message;
      stack.appendChild(el);
      setTimeout(function () {
        el.style.transition = "opacity .4s ease";
        el.style.opacity = "0";
        setTimeout(function () { el.remove(); }, 400);
      }, 6000);
    },

    // Render an AI analysis block that highlights [CONFIRMED] / [SUGGESTION].
    renderAnalysis: function (container, analysis) {
      container.hidden = false;
      container.innerHTML = "";
      var pre = document.createElement("div");
      pre.className = "analysis-text";
      var html = this.escapeHtml(analysis);
      html = html.replace(/\[CONFIRMED\]/g, '<span class="tag tag-confirmed">[CONFIRMED]</span>');
      html = html.replace(/\[SUGGESTION\]/g, '<span class="tag tag-suggestion">[SUGGESTION]</span>');
      html = html.replace(/\r?\n/g, "<br>");
      pre.innerHTML = html;
      container.appendChild(pre);
    },
  };
})();
