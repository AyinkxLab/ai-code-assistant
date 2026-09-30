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
      html = html.replace(/\r?\n/g, "<er>");
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
      html = html.replace(/\r?\n/g, "<er>");
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
        else if (line.indexOf("---") === 0 || line.indexOf("+++") === 0) cls += " diff-meta";
        else if (line.charAt(0) === "+") cls += " diff-added";
        else if (line.charAt(0) === "-") cls += " diff-removed";
        return '<span class="' + cls + '">' + this.escapeHtml(line) + "</span>";
      }, this).join("");
      return '<div class="code-view diff">' + html + "</div>";
    },

    // Render the GitHub integration help panel contained in the docs.
    // This is the client-side counterpart of the documentation added for
    // the GitHub integration guide: it explains the OAuth setup, the
    // environment variables, the encrypted-token storage design, the API
    // client's retry/rate-limit behaviour, and common troubleshooting.
    renderIntegrationDocs: function (container) {
      if (!container) return;
      container.innerHTML = "";
      var sections = [
        {
          id: "github-setup",
          title: "Setup",
          html:
            "<p>The setup steps mirror the <strong>README</strong> GitHub section. " +
            "Create an OAuth app in GitHub Settings → <em>Developer settings</em> – "+
            "<em>OAuth Apps</em>, then configure the callback URL to "+
            "<code>/github/callback</code> and set the environment variables " +
            "below.</p>" +
            "<ul>" +
            "<li><code>GITHUB_CLIENT_ID</code> — OAuth app client ID.</li>" +
            "<li><code>GITHUB_CLIENT_SECRET</code> — OAuth app client secret.</li>" +
            "<li><code>GITHUB_REDIRECT_URI</code> — Must match the callback URL exactly.</li>" +
            "<li><code>GITHUB_SCOPES</code> — Comma-separated scopes (e.g. <code>repo,read_user</code>).</li>" +
            "<li><code>TOKEN_ENCRYPTION_KEY</code> — Base64-encoded 32-byte key used by " +
            "<code>app/services/crypto.py</code>.</li>" +
            "</ul>",
        },
        {
          id: "github-architecture",
          title: "Architecture",
          html:
            "<p>The integration is split across three layers:</p>" +
            "<ul>" +
            "<li><code>app/services/github.py</code> — the GitHub API " +
            "client. It handles request building, auth headers, retries, and " +
            "rate-limit backoff.</li>" +
            "<li><code>app/services/crypto.py</code> — symmetric encryption " +
            "for tokens at rest. Tokens are never stored in plaintext.</li>" +
            "<li><code>app/github/routes.py</code> — the HTTP endpoints " +
            "that drive the connect/callback flow and expose the issues/pulls " +
            "APIs to the frontend.</li>" +
            "</ul>",
        },
        {
          id: "github-troubleshooting",
          title: "Troubleshooting",
          html:
            "<ul>" +
            "<li><strong>Scope missing</strong> — Re-connect and grant the " +
            "required scopes; the API will return <code>403</code> with a " +
            "<code>scope</code> error until the token is reissued.</li>" +
            "<li><strong>Rate limits</strong> — the client retries on " +
            "<code>429</code> and <code>503</code> with exponential backoff " +
            "and honours <code>Retry-After</code>; if the budget is exhausted the " +
            "call fails with a rate-limit error.</li>" +
            "<li><strong>Token revoked</strong> — the API returns <code>401</code> " +
            "with <code>kind: 'auth'</code>; the frontend redirects to " +
            "<code>/github/connect</code> to re-authorize.</li>" +
            "</ul>",
        },
        {
          id: "github-security",
          title: "Security note",
          html:
            "<p>Tokens are encrypted at rest using <code>app/services/crypto.py</code> " +
            "and decrypted only in memory when a request is made. They are " +
            "never logged, never returned to the browser, and never included " +
            "in error messages. Rotate <code>TOKEN_ENCRYPTION_KEY</code> " +
            "carefully and revoke access from GitHub when a connection is " +
            "no longer needed.</p>",
        },
      ];
      sections.forEach(function (section) {
        var wrapper = document.createElement("section");
        wrapper.id = section.id;
        wrapper.className = "docs-section";
        var heading = document.createElement("h2");
        heading.textContent = section.title;
        wrapper.appendChild(heading);
        var body = document.createElement("div");
        body.className = "docs-body";
        body.innerHTML = section.html;
        wrapper.appendChild(body);
        container.appendChild(wrapper);
      });
    },
  };
})();
