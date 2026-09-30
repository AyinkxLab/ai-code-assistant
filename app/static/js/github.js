// AI Code Assistant — GitHub integration shared helpers
// Reused by all GitHub pages: CSRF-aware API client, escaping, and rendering.

(function () {
  "use strict";

  var CANCELLED_CODES = { access_denied: true };

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

    // Escape HTML, then treat newlines as <er> and URLs as links.
    renderMarkdownish: function (text) {
      if (!text) return "";
      var html = this.escapeHtml(text);
      html = html.replace(/(^|\s)(https?:\/\/[^\s<]+)/g, '$1<a href="$2" target="_blank" rel="noopener">$2</a>');
      html = html.replace(/\r?\n/g, "<br>");
      return html;
    },

    // True when an OAuth callback error code represents the user denying
    // consent (a cancellation), not a failure.
    isCancellation: function (code) {
      return !!code && Object.prototype.hasOwnProperty.call(CANCELLED_CODES, String(code));
    },

    // Read the callback error from the URL without exposing the raw GitHub
    // payload. Only the code and a safe description are returned.
    parseCallbackError: function (search) {
      var params = new URLSearchParams(search == null ? window.location.search : search);
      var code = params.get("error");
      if (!code) return null;
      return {
        code: code,
        cancelled: this.isCancellation(code),
        // Never return the full GitHub error description/URI; only a
        // fixed, non-leaking explanation.
        message: this.isCancellation(code)
          ? "You canceled the GitHub connection."
          : "GitHub could not complete the connection.",
      };
    },

    // Remove the callback error parameters from the address bar so a reload
    // does not re-surface the failure.
    clearCallbackError: function () {
      if (!window.history || !window.history.replaceState) return;
      var params = new URLSearchParams(window.location.search);
      if (!params.has("error") && !params.has("error_description") && !params.has("error_uri")) return;
      params.delete("error");
      params.delete("error_description");
      params.delete("error_uri");
      var query = params.toString();
      window.history.replaceState({}, "", window.location.pathname + (query ? "?" + query : "") + window.location.hash);
    },

    // Render a friendly cancellation/error screen into container. The
    // explanation lists what access will be requested and offers a
    // retry button back to the connect flow.
    renderConnectStatus: function (container, info) {
      if (!container) return;
      container.innerHTML = "";
      container.hidden = false;

      var card = document.createElement("div");
      card.className = "card connect-status";

      var title = document.createElement("h1");
      title.textContent = info && info.cancelled ? "GitHub connection canceled" : "GitHub connection failed";
      card.appendChild(title);

      var lead = document.createElement("p");
      lead.className = "field-hint";
      lead.textContent = info && info.message
        ? info.message
        : "GitHub could not complete the connection.";
      card.appendChild(lead);

      var explain = document.createElement("p");
      explain.textContent = "Connecting GitHub grants this app read access to your repositories, issues, and pull requests so it can assist you. You can revoke access at any time from your GitHub settings.";
      card.appendChild(explain);

      var actions = document.createElement("div");
      actions.className = "connect-actions";
      var retry = document.createElement("a");
      retry.className = "btn btn-primary";
      retry.href = "/github/connect";
      retry.textContent = "Connect GitHub";
      actions.appendChild(retry);
      card.appendChild(actions);

      container.appendChild(card);
    },

    // Initialize the GitHub dashboard: surface a friendly cancellation/error
    // screen with a retry path when the OAuth callback reports a failure.
    initDashboard: function () {
      var container = document.querySelector("[data-github-connect-status]");
      if (!container) return;
      var info = this.parseCallbackError();
      if (!info) return;
      this.renderConnectStatus(container, info);
      this.clearCallbackError();
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

    // Render Prev/Next controls for a paginated GitHub list endpoint.
    // `meta` is the {page, per_page, has_prev, has_next, total_pages} envelope
    // returned by the issues/pulls APIs; `onPage(pageNumber)` is invoked when
    // the user navigates. Preserves the current filters because the caller owns
    // them and re-requests with the new page.
    renderPager: function (container, meta, onPage) {
      if (!container) return;
      container.innerHTML = "";
      var page = meta.page || 1;
      var label = "Page " + page;
      if (meta.total_pages) label += " of " + meta.total_pages;

      var prev = document.createElement("button");
      prev.type = "button";
      prev.className = "btn btn-ghost btn-sm";
      prev.textContent = "Previous";
      prev.disabled = !meta.has_prev;
      prev.addEventListener("click", function () { onPage(page - 1); });

      var info = document.createElement("span");
      info.className = "field-hint";
      info.textContent = label;

      var next = document.createElement("button");
      next.type = "button";
      next.className = "btn btn-ghost btn-sm";
      next.textContent = "Next";
      next.disabled = !meta.has_next;
      next.addEventListener("click", function () { onPage(page + 1); });

      container.appendChild(prev);
      container.appendChild(info);
      container.appendChild(next);
    },

    // Render a "Load more" control that appends the next page (issue #66).
    // `meta` is a list envelope with `has_next`; the button is removed once the
    // last page has been reached. `onLoadMore()` is invoked to fetch + append.
    renderLoadMore: function (container, meta, onLoadMore) {
      if (!container) return;
      var existing = container.querySelector(".load-more-row");
      if (existing) existing.remove();
      if (!meta || !meta.has_next) return;
      var row = document.createElement("div");
      row.className = "load-more-row";
      var button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-ghost btn-sm";
      button.textContent = "Load more";
      button.addEventListener("click", function () {
        button.disabled = true;
        button.textContent = "Loading...";
        onLoadMore();
      });
      row.appendChild(button);
      container.appendChild(row);
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

    // Render a unified diff patch with per-line color coding.
    // Added/removed lines get their own class; hunk headers and file headers
    // are dimmed. Every line is HTML-escaped before insertion.
    renderPatch: function (patch) {
      if (!patch) return '<div class="code-view diff">(no inline diff available)</div>';
      var lines = String(patch).replace(/\r\n/g, "\n").split("\n");
      var html = lines.map(function (line) {
        var cls = "diff-line";
        if (line.indexOf("@@") === 0) cls += " diff-hunk";
        else if (line.indexOf("+++") === 0 || line.indexOf("---") === 0) cls += " diff-meta";
        else if (line.charAt(0) === "+") cls += " diff-added";
        else if (line.charAt(0) === "-") cls += " diff-removed";
        return '<span class="' + cls + '">' + this.escapeHtml(line) + "</span>";
      }, this).join("");
      return '<div class="code-view diff">' + html + "</div>";
    },
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", function () {
      window.GitHub.initDashboard();
    });
  } else {
    window.GitHub.initDashboard();
  }
})();
