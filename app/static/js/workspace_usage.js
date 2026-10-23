// AI Code Assistant - workspace usage dashboard states (#456)
//
// The workspace usage panel is loaded asynchronously. Every state must be
// explicit: loading, empty, populated, or an error with a retry affordance.
// A failed request must never leave the previous "Loading..." placeholders on
// screen, and an empty workspace must not render fabricated zero figures.

(function (root, factory) {
  "use strict";
  var api = factory();
  if (typeof module === "object" && module.exports) {
    module.exports = api;
  } else {
    root.AICAWorkspaceUsage = api;
  }
})(typeof self !== "undefined" ? self : this, function () {
  "use strict";

  var TABLE_COLUMNS = {
    "workspace-usage-projects": 3,
    "workspace-usage-sources": 3,
    "workspace-usage-members": 4,
  };

  function escapeHtml(value) {
    return String(value == null ? "" : value).replace(/[&<>"']/g, function (character) {
      return {
        "&": "&amp;",
        "<": "&lt;",
        ">": "&gt;",
        '"': "&quot;",
        "'": "&#39;",
      }[character];
    });
  }

  function formatTokens(value) {
    return Number(value || 0).toLocaleString("en-US");
  }

  function formatCost(value, currency) {
    if (value == null) return "\u2014";
    var view = typeof window !== "undefined" ? window : null;
    if (view && view.AICACurrency && typeof view.AICACurrency.formatCost === "function") {
      try {
        return view.AICACurrency.formatCost(value, currency);
      } catch (error) {
        return String(value);
      }
    }
    return String(value);
  }

  function stateHtml(kind, options) {
    options = options || {};
    kind = kind || "loading";

    if (kind === "loading") {
      return (
        '<div class="usage-state usage-state-loading" role="status" aria-live="polite">' +
        '<p class="empty-note">' +
        escapeHtml(options.message || "Loading usage...") +
        "</p></div>"
      );
    }

    var title = options.title
      ? '<p class="usage-state-title">' + escapeHtml(options.title) + "</p>"
      : "";
    var message = options.message
      ? '<p class="empty-note">' + escapeHtml(options.message) + "</p>"
      : "";
    var retry = options.retry
      ? '<button type="button" class="btn btn-ghost btn-sm usage-retry">' +
        escapeHtml(options.retryLabel || "Retry") +
        "</button>"
      : "";
    var role = kind === "empty" ? "status" : "alert";

    return (
      '<div class="usage-state usage-state-' +
      escapeHtml(kind) +
      '" role="' +
      role +
      '">' +
      title +
      message +
      retry +
      "</div>"
    );
  }

  function errorStateHtml(options) {
    options = options || {};
    return stateHtml("error", {
      title: options.title || "Could not load usage",
      message:
        options.message ||
        "We couldn't load workspace usage right now. Please try again.",
      retry: options.retry !== false,
      retryLabel: options.retryLabel,
    });
  }

  function bindRetry(container, handler) {
    if (!container) return;
    var button = container.querySelector(".usage-retry");
    if (button) button.addEventListener("click", handler);
  }

  function setBusy(doc, busy) {
    var panel = doc.getElementById("workspace-usage-panel");
    if (panel) panel.setAttribute("aria-busy", busy ? "true" : "false");
  }

  function setNote(doc, text) {
    var note = doc.getElementById("workspace-usage-note");
    if (note) note.textContent = text;
  }

  function setTableState(doc, id, message) {
    var body = doc.getElementById(id);
    if (!body) return;
    var columns = TABLE_COLUMNS[id] || 3;
    body.innerHTML =
      '<tr><td colspan="' +
      columns +
      '" class="empty-note">' +
      escapeHtml(message) +
      "</td></tr>";
  }

  function loading(doc) {
    setBusy(doc, true);
    var totals = doc.getElementById("workspace-usage-totals");
    if (totals) totals.innerHTML = stateHtml("loading", { message: "Loading usage..." });
    setNote(doc, "Token and cost totals across this workspace. Costs are estimates.");
    Object.keys(TABLE_COLUMNS).forEach(function (id) {
      setTableState(doc, id, "Loading...");
    });
  }

  function metricCard(label, value) {
    return (
      '<div class="metric-card"><span class="metric-label">' +
      escapeHtml(label) +
      '</span><strong class="metric-value">' +
      escapeHtml(value) +
      "</strong></div>"
    );
  }

  function rowHtml(cells) {
    return (
      "<tr>" +
      cells
        .map(function (value, index) {
          return index === 0
            ? '<td scope="row">' + escapeHtml(value) + "</td>"
            : "<td>" + escapeHtml(value) + "</td>";
        })
        .join("") +
      "</tr>"
    );
  }

  function rowsOrEmpty(rows, columns, cells, emptyMessage) {
    if (!rows.length) {
      return (
        '<tr><td colspan="' +
        columns +
        '" class="empty-note">' +
        escapeHtml(emptyMessage) +
        "</td></tr>"
      );
    }
    return rows.map(function (row) {
      return rowHtml(cells(row));
    }).join("");
  }

  function render(doc, data) {
    data = data || {};
    setBusy(doc, false);

    var currency = data.currency || null;
    var totals = data.totals || {};
    var projects = Array.isArray(data.by_project) ? data.by_project : [];
    var sources = Array.isArray(data.by_source) ? data.by_source : [];
    var members = Array.isArray(data.by_member) ? data.by_member : [];
    var showMembers = data.member_detail === "all";
    var hasUsage =
      Number(totals.count || 0) > 0 ||
      projects.length > 0 ||
      sources.length > 0 ||
      (showMembers && members.length > 0);

    var totalsEl = doc.getElementById("workspace-usage-totals");
    if (totalsEl) {
      totalsEl.innerHTML = hasUsage
        ? metricCard("Total tokens", formatTokens(totals.total_tokens)) +
          metricCard("Requests", formatTokens(totals.count)) +
          metricCard("Estimated cost", formatCost(totals.cost_estimate, currency))
        : stateHtml("empty", {
            title: "No usage yet",
            message: "No usage has been recorded in this workspace yet.",
          });
    }

    var projectBody = doc.getElementById("workspace-usage-projects");
    if (projectBody) {
      projectBody.innerHTML = rowsOrEmpty(
        projects,
        3,
        function (row) {
          var rowTotals = row.totals || {};
          return [
            row.name,
            formatTokens(rowTotals.total_tokens),
            formatCost(rowTotals.cost_estimate, currency),
          ];
        },
        "No project usage yet."
      );
    }

    var sourceBody = doc.getElementById("workspace-usage-sources");
    if (sourceBody) {
      sourceBody.innerHTML = rowsOrEmpty(
        sources,
        3,
        function (row) {
          var rowTotals = row.totals || {};
          return [
            row.source,
            formatTokens(rowTotals.total_tokens),
            formatCost(rowTotals.cost_estimate, currency),
          ];
        },
        "No usage recorded yet."
      );
    }

    if (showMembers) {
      var memberBody = doc.getElementById("workspace-usage-members");
      if (memberBody) {
        memberBody.innerHTML = rowsOrEmpty(
          members,
          4,
          function (row) {
            var rowTotals = row.totals || {};
            return [
              row.username,
              row.role || "member",
              formatTokens(rowTotals.total_tokens),
              formatCost(rowTotals.cost_estimate, currency),
            ];
          },
          "No member usage yet."
        );
      }
    }

    setNote(
      doc,
      "Token and cost totals across this workspace. Costs are estimates." +
        (data.member_detail === "self" ? " Member breakdown shows your usage only." : "")
    );
  }

  function failure(doc) {
    setBusy(doc, false);
    var totals = doc.getElementById("workspace-usage-totals");
    if (totals) totals.innerHTML = errorStateHtml();
    Object.keys(TABLE_COLUMNS).forEach(function (id) {
      setTableState(doc, id, "Usage unavailable.");
    });
    setNote(doc, "Usage is unavailable right now. Use Retry to try again.");
    return totals;
  }

  function load(options) {
    options = options || {};
    var doc =
      options.document || (typeof document !== "undefined" ? document : null);
    var fetchUsage = options.fetchUsage;
    if (!doc || typeof fetchUsage !== "function") {
      return Promise.resolve(null);
    }

    loading(doc);
    return Promise.resolve()
      .then(fetchUsage)
      .then(function (data) {
        render(doc, data || {});
        return data;
      })
      .catch(function (error) {
        var totals = failure(doc);
        bindRetry(totals, function () {
          load(options);
        });
        if (typeof options.onError === "function") options.onError(error);
        return null;
      });
  }

  return {
    bindRetry: bindRetry,
    errorStateHtml: errorStateHtml,
    failure: failure,
    formatCost: formatCost,
    formatTokens: formatTokens,
    load: load,
    loading: loading,
    render: render,
    stateHtml: stateHtml,
  };
});
