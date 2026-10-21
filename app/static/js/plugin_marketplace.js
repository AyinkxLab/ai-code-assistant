// AI Code Assistant — shared marketplace UI helpers (#418, #421)
//
// The marketplace surfaces (plugin list, lockfile viewer and plugin detail)
// share one implementation of the empty, error and timeout states plus a
// timeout-bounded JSON fetch. Keeping these states consistent matters: every
// failure must be friendly, explain what happened, and offer a retry instead of
// leaking a raw exception or leaving a blank panel.

(function () {
  "use strict";

  var DEFAULT_TIMEOUT_MS = 10000;

  function escapeHtml(text) {
    return String(text == null ? "" : text).replace(/[&<>"']/g, function (c) {
      return { "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c];
    });
  }

  // A catalog source is a URL, but it is still operator-supplied data. Strip
  // embedded credentials before rendering so the lockfile viewer can never
  // echo a user:password pair from a malformed catalog entry.
  function safeSource(value) {
    var text = String(value == null ? "" : value);
    try {
      var url = new URL(text);
      if (url.username || url.password) {
        url.username = "";
        url.password = "";
        return url.toString();
      }
    } catch (e) {
      // Not an absolute URL (for example a relative catalog path); render as-is.
    }
    return text;
  }

  function stateHtml(kind, options) {
    options = options || {};
    kind = kind || "loading";

    if (kind === "loading") {
      return (
        '<div class="marketplace-state marketplace-state-loading" role="status" aria-live="polite">' +
        '<p class="empty-note">' +
        escapeHtml(options.message || "Loading…") +
        "</p></div>"
      );
    }

    var title = escapeHtml(options.title || "");
    var message = escapeHtml(options.message || "");
    var retry = options.retry
      ? '<button type="button" class="btn btn-ghost btn-sm marketplace-retry">' +
        escapeHtml(options.retryLabel || "Retry") +
        "</button>"
      : "";
    var role = kind === "empty" ? "status" : "alert";

    return (
      '<div class="marketplace-state marketplace-state-' +
      escapeHtml(kind) +
      '" role="' +
      role +
      '">' +
      (title ? '<p class="marketplace-state-title">' + title + "</p>" : "") +
      (message ? '<p class="empty-note">' + message + "</p>" : "") +
      retry +
      "</div>"
    );
  }

  function errorKind(error) {
    return error && error.code === "timeout" ? "timeout" : "error";
  }

  function errorStateHtml(error, options) {
    options = options || {};
    var timeout = errorKind(error) === "timeout";
    return stateHtml(timeout ? "timeout" : "error", {
      title:
        options.title ||
        (timeout ? "This is taking longer than expected" : "Something went wrong"),
      message:
        options.message ||
        (timeout
          ? "The marketplace did not respond in time. Check your connection and try again."
          : "We couldn’t load the marketplace right now. Please try again."),
      retry: options.retry !== false,
      retryLabel: options.retryLabel,
    });
  }

  function fetchJson(url, options) {
    options = options || {};
    var timeoutMs = options.timeoutMs || DEFAULT_TIMEOUT_MS;
    var controller = typeof AbortController !== "undefined" ? new AbortController() : null;
    var timer = setTimeout(function () {
      if (controller) controller.abort();
    }, timeoutMs);
    var fetchOptions = Object.assign({}, options);
    delete fetchOptions.timeoutMs;
    if (controller) fetchOptions.signal = controller.signal;

    return fetch(url, fetchOptions)
      .then(function (response) {
        return response
          .json()
          .catch(function () {
            return {};
          })
          .then(function (data) {
            if (!response.ok) {
              var error = new Error(
                data && data.error
                  ? data.error
                  : "Request failed (" + response.status + ")."
              );
              error.code = "http";
              error.status = response.status;
              throw error;
            }
            return data;
          });
      })
      .catch(function (error) {
        if (error && error.name === "AbortError") {
          var timeout = new Error("The request timed out.");
          timeout.code = "timeout";
          throw timeout;
        }
        if (error && !error.code) error.code = "network";
        throw error;
      })
      .finally(function () {
        clearTimeout(timer);
      });
  }

  function renderLockfile(data) {
    var entries = data && Array.isArray(data.plugins) ? data.plugins : [];
    if (!entries.length) {
      return stateHtml("empty", {
        title: "No plugins pinned yet",
        message:
          "Install a plugin from the catalog to record its resolved version and hash in this workspace lockfile.",
      });
    }

    var rows = entries
      .map(function (entry) {
        var dependencies =
          Array.isArray(entry.dependencies) && entry.dependencies.length
            ? entry.dependencies.join(", ")
            : "none";
        return (
          "<tr>" +
          '<td><code>' +
          escapeHtml(entry.plugin_id) +
          "</code></td>" +
          "<td>v" +
          escapeHtml(entry.version) +
          "</td>" +
          '<td><code class="plugin-lockfile-hash" title="' +
          escapeHtml(entry.integrity) +
          '">' +
          escapeHtml(entry.integrity) +
          "</code></td>" +
          '<td><span class="activity-meta">' +
          escapeHtml(safeSource(entry.source) || "unknown") +
          "</span></td>" +
          '<td><span class="activity-meta">' +
          escapeHtml(dependencies) +
          "</span></td>" +
          "</tr>"
        );
      })
      .join("");

    return (
      '<p class="activity-meta plugin-lockfile-summary">' +
      escapeHtml(entries.length) +
      " pinned plugin(s) · lockfile version " +
      escapeHtml((data && data.lockfile_version) || 1) +
      "</p>" +
      '<div class="table-scroll">' +
      '<table class="plugin-lockfile-table">' +
      "<thead><tr>" +
      "<th scope=\"col\">Plugin</th>" +
      "<th scope=\"col\">Version</th>" +
      "<th scope=\"col\">Integrity</th>" +
      "<th scope=\"col\">Source</th>" +
      "<th scope=\"col\">Dependencies</th>" +
      "</tr></thead><tbody>" +
      rows +
      "</tbody></table></div>"
    );
  }

  function bindRetry(container, handler) {
    if (!container) return;
    var button = container.querySelector(".marketplace-retry");
    if (button) button.addEventListener("click", handler);
  }

  function progressHtml(message) {
    return (
      '<span class="marketplace-progress" role="status" aria-live="polite">' +
      '<span class="marketplace-spinner" aria-hidden="true"></span>' +
      '<span class="marketplace-progress-text">' +
      escapeHtml(message || "Working\u2026") +
      "</span></span>"
    );
  }

  // Long-running installs/updates/rollbacks stay non-blocking because the
  // browser keeps painting while the request is in flight; these helpers make
  // that visible and mark the region busy for assistive technology.
  function busyOn(element, message) {
    if (!element) return;
    element.hidden = false;
    element.setAttribute("aria-busy", "true");
    element.innerHTML = progressHtml(message);
  }

  function busyOff(element) {
    if (!element) return;
    element.hidden = true;
    element.removeAttribute("aria-busy");
    element.innerHTML = "";
  }

  function busyError(element, error) {
    if (!element) return;
    element.hidden = false;
    element.removeAttribute("aria-busy");
    element.innerHTML = errorStateHtml(error, {
      title: "The action failed",
      message:
        error && error.code === "timeout"
          ? "The request timed out. Please try again."
          : error && error.message
            ? error.message
            : "Please try again.",
    });
  }

  window.AICAPluginMarketplace = {
    DEFAULT_TIMEOUT_MS: DEFAULT_TIMEOUT_MS,
    bindRetry: bindRetry,
    busyError: busyError,
    busyOff: busyOff,
    busyOn: busyOn,
    errorKind: errorKind,
    errorStateHtml: errorStateHtml,
    escapeHtml: escapeHtml,
    fetchJson: fetchJson,
    progressHtml: progressHtml,
    renderLockfile: renderLockfile,
    safeSource: safeSource,
    stateHtml: stateHtml,
  };
})();
