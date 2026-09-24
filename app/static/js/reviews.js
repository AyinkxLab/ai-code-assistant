// AI Code Assistant — Reviews: history, detail, project reviews, and config.
(function () {
  "use strict";

  var GH = window.GitHub;
  var SEVERITY_ORDER = ["critical", "high", "medium", "low", "informational"];

  function projectId() {
    var el = document.querySelector(".github-header");
    return el ? el.getAttribute("data-project-id") : null;
  }

  function reviewId() {
    var el = document.querySelector(".github-header");
    return el ? el.getAttribute("data-review-id") : null;
  }

  function severityClass(severity) {
    return "badge-severity-" + (SEVERITY_ORDER.indexOf(severity) >= 0 ? severity : "medium");
  }

  function badge(severity) {
    return '<span class="tag ' + severityClass(severity) + '">' + GH.escapeHtml(severity) + "</span>";
  }

  function sourceLabel(review) {
    if (review.source === "github_pr") {
      return review.owner + "/" + review.repo + " #" + (review.pr_number || "");
    }
    return review.kind + " review";
  }

  function statusLabel(review) {
    var cls = review.status === "completed" ? "tag-public" : review.status === "failed" ? "tag-private" : "";
    return '<span class="tag ' + cls + '">' + GH.escapeHtml(review.status) + "</span>";
  }

  function reviewCard(review) {
    var meta = sourceLabel(review);
    return (
      '<a class="issue-item" href="/reviews/' + review.id + '">' +
      '<div class="issue-item-title">' +
      statusLabel(review) + " " + GH.escapeHtml(meta) +
      "</div>" +
      '<div class="issue-item-sub">' +
      "kind " + GH.escapeHtml(review.kind) + " &middot; " +
      (review.findings_count || 0) + " findings &middot; " +
      GH.relativeDate(review.created_at) +
      "</div>" +
      "</a>"
    );
  }

  // ----- Quality dashboard strip ----------------------------------------

  function metricCard(value, label) {
    return (
      "<div class='metric-card'>" +
      "<span class='metric-value'>" + value + "</span>" +
      "<span class='metric-label'>" + label + "</span>" +
      "</div>"
    );
  }

  function breakdown(title, counts, limit) {
    var keys = Object.keys(counts || {});
    if (!keys.length) return "";
    var html = "<h3 class='metric-title'>" + GH.escapeHtml(title) + "</h3><ul class='metric-list'>";
    keys.slice(0, limit || keys.length).forEach(function (key) {
      html += "<li><code>" + GH.escapeHtml(key) + "</code> — " + counts[key] + "</li>";
    });
    return html + "</ul>";
  }

  function trendSection(trend) {
    if (!trend || !trend.length) return "";
    var max = 0;
    trend.forEach(function (point) { max = Math.max(max, point.total || 0); });
    max = max || 1;
    var html = "<h3 class='metric-title'>Findings addressed over time</h3>" +
      "<p class='repo-meta'>Open vs addressed findings for each of the last " +
      trend.length + " review" + (trend.length === 1 ? "" : "s") + ".</p>" +
      "<ul class='trend-list'>";
    trend.forEach(function (point) {
      var openPct = Math.round(((point.open || 0) / max) * 100);
      var donePct = Math.round(((point.addressed || 0) / max) * 100);
      html += "<li class='trend-row'>" +
        "<span class='trend-date'>" + GH.escapeHtml(GH.relativeDate(point.created_at)) + "</span>" +
        "<span class='trend-bar' title='" + (point.open || 0) + " open / " +
        (point.addressed || 0) + " addressed'>" +
        "<span class='trend-open' style='width:" + openPct + "%'></span>" +
        "<span class='trend-done' style='width:" + donePct + "%'></span>" +
        "</span>" +
        "<span class='trend-counts'>" + (point.open || 0) + " open &middot; " +
        (point.addressed || 0) + " addressed</span>" +
        "</li>";
    });
    return html + "</ul>";
  }

  function renderMetrics(metrics, container) {
    if (!container) return;
    var findings = metrics.findings || {};
    var html = "<div class='metric-grid'>" +
      metricCard(metrics.total_reviews || 0, "reviews") +
      metricCard(findings.total || 0, "findings") +
      metricCard(findings.high_risk || 0, "high risk") +
      metricCard(findings.unaddressed_high_risk || 0, "open high risk") +
      metricCard(findings.addressed || 0, "addressed") +
      metricCard(metrics.reviews_last_7_days || 0, "reviews / 7d") +
      "</div>";
    html += trendSection(metrics.findings_trend);
    html += breakdown("Findings by severity", findings.by_severity);
    html += breakdown("Findings by category", findings.by_category, 10);
    if (metrics.last_review_at) {
      html += "<p class='repo-meta'>Last review " + GH.relativeDate(metrics.last_review_at) +
        " &middot; " + (metrics.reviews_last_30_days || 0) + " in the last 30 days</p>";
    }
    container.innerHTML = html;
  }

  function loadMetrics(container) {
    var pid = projectId();
    var url = "/reviews/api/metrics" + (pid ? "?project_id=" + encodeURIComponent(pid) : "");
    GH.api(url).then(function (metrics) {
      renderMetrics(metrics, container);
    }).catch(function () {
      container.innerHTML = '<p class="sidebar-empty">Quality metrics unavailable.</p>';
    });
  }

  // ----- Index / history -------------------------------------------------

  function loadReviewList(container, filter) {
    var params = [];
    if (filter) {
      if (filter.source) params.push("source=" + encodeURIComponent(filter.source));
      if (filter.kind) params.push("kind=" + encodeURIComponent(filter.kind));
      if (filter.project_id) params.push("project_id=" + encodeURIComponent(filter.project_id));
    }
    var url = "/reviews/api/reviews" + (params.length ? "?" + params.join("&") : "");
    GH.api(url).then(function (reviews) {
      container.innerHTML = "";
      if (!reviews.length) {
        container.innerHTML =
          '<p class="sidebar-empty">No reviews yet. Run a review from a project or a pull request.</p>';
        return;
      }
      reviews.forEach(function (review) {
        var el = document.createElement("div");
        el.innerHTML = reviewCard(review);
        container.appendChild(el.firstChild);
      });
    }).catch(function (error) {
      container.innerHTML = '<p class="sidebar-empty">Could not load reviews.</p>';
      GH.flashError(error.message);
    });
  }

  function initIndex() {
    var metricsEl = document.getElementById("metrics-strip");
    var listEl = document.getElementById("review-list");
    if (!listEl) return;
    loadMetrics(metricsEl);
    loadReviewList(listEl, null);
  }

  // ----- Detail ----------------------------------------------------------

  function findingsUrl(base) {
    var severity = document.getElementById("finding-severity");
    var category = document.getElementById("finding-category");
    var confidence = document.getElementById("finding-confidence");
    var addressed = document.getElementById("finding-addressed");
    var params = [];
    if (severity && severity.value) params.push("severity=" + encodeURIComponent(severity.value));
    if (category && category.value) params.push("category=" + encodeURIComponent(category.value));
    if (confidence && confidence.value) params.push("confidence=" + encodeURIComponent(confidence.value));
    if (addressed && addressed.value !== "") params.push("addressed=" + encodeURIComponent(addressed.value));
    return base + (params.length ? "?" + params.join("&") : "");
  }

  function populateCategories(categories) {
    var select = document.getElementById("finding-category");
    if (!select) return;
    select.innerHTML = '<option value="">All categories</option>';
    (categories || []).forEach(function (category) {
      var option = document.createElement("option");
      option.value = category;
      option.textContent = category;
      select.appendChild(option);
    });
  }

  function summarySection(title, items) {
    if (!items || !items.length) return "";
    return (
      "<h4>" + GH.escapeHtml(title) + "</h4><ul>" +
      items.map(function (item) {
        return "<li>" + GH.renderMarkdownish(item) + "</li>";
      }).join("") +
      "</ul>"
    );
  }

  function confidenceTag(finding) {
    var confirmed = finding.confidence === "confirmed";
    var label = finding.confidence_label || (confirmed ? "[CONFIRMED]" : "[SUGGESTION]");
    return (
      '<span class="tag ' + (confirmed ? "tag-confirmed" : "tag-suggestion") + '">' +
      GH.escapeHtml(label) + "</span>"
    );
  }

  function findingCard(finding) {
    var location = finding.file ? GH.escapeHtml(finding.file) : "(whole repo)";
    if (finding.line != null) location += ":" + finding.line;
    return (
      '<div class="finding-card">' +
      '<div class="finding-header">' +
      badge(finding.severity) +
      " " + confidenceTag(finding) +
      " " + GH.escapeHtml(finding.category) +
      " <span class='finding-location'>" + location + "</span>" +
      '<button class="btn btn-ghost btn-sm finding-toggle" data-id="' + finding.id +
      '" data-addressed="' + (finding.addressed ? "0" : "1") + '" type="button">' +
      (finding.addressed ? "Reopen" : "Mark addressed") + "</button>" +
      "</div>" +
      "<p>" + GH.renderMarkdownish(finding.explanation) + "</p>" +
      (finding.recommendation
        ? "<p class='finding-recommendation'><strong>Recommendation:</strong> " +
          GH.renderMarkdownish(finding.recommendation) + "</p>"
        : "") +
      "</div>"
    );
  }

  function loadFindings(reviewId) {
    var container = document.getElementById("finding-list");
    var countEl = document.getElementById("finding-count");
    container.innerHTML = '<p class="sidebar-empty">Loading findings...</p>';
    GH.api(findingsUrl("/reviews/api/reviews/" + reviewId + "/findings"))
      .then(function (findings) {
        container.innerHTML = "";
        if (countEl) countEl.textContent = findings.length + " finding" + (findings.length === 1 ? "" : "s");
        if (!findings.length) {
          container.innerHTML = '<p class="sidebar-empty">No findings match.</p>';
          return;
        }
        findings.slice().sort(function (a, b) {
          var ra = SEVERITY_ORDER.indexOf(a.severity);
          var rb = SEVERITY_ORDER.indexOf(b.severity);
          return (ra < 0 ? 99 : ra) - (rb < 0 ? 99 : rb);
        }).forEach(function (finding) {
          var el = document.createElement("div");
          el.innerHTML = findingCard(finding);
          container.appendChild(el.firstChild);
        });
      })
      .catch(function (error) {
        container.innerHTML = '<p class="sidebar-empty">Could not load findings.</p>';
        GH.flashError(error.message);
      });
  }

  function loadDetail() {
    var id = reviewId();
    var container = document.getElementById("review-detail");
    if (!container || !id) return;
    GH.api("/reviews/api/reviews/" + id).then(function (review) {
      var summary = review.summary || {};
      var title = review.source === "github_pr"
        ? review.owner + "/" + review.repo + " #" + (review.pr_number || "") + " — " + (review.pr_title || "")
        : review.kind + " review";
      container.innerHTML =
        '<div class="issue-detail-header">' +
        "<h2>" + GH.escapeHtml(title) + "</h2>" +
        '<div class="issue-meta">' +
        statusLabel(review) + " " + GH.escapeHtml(review.kind) + " &middot; " +
        (review.findings_count || 0) + " findings &middot; " + GH.relativeDate(review.created_at) +
        "</div>" +
        (review.error_message ? '<p class="flash flash-error">' + GH.escapeHtml(review.error_message) + "</p>" : "") +
        "</div>" +
        '<div class="analysis-output">' +
        summarySection("Overall assessment", summary.overall_assessment ? [summary.overall_assessment] : []) +
        summarySection("Important findings", summary.important_findings) +
        summarySection("Suggested improvements", summary.suggested_improvements) +
        summarySection("Testing recommendations", summary.testing_recommendations) +
        summarySection("Security concerns", summary.security_concerns) +
        summarySection("Performance concerns", summary.performance_concerns) +
        summarySection("Files affected", summary.files_affected) +
        (review.status === "failed" ? '<p class="sidebar-empty">This review did not complete.</p>' : "") +
        "</div>";
      populateCategories(review.categories);
      loadFindings(id);
    }).catch(function (error) {
      container.innerHTML = '<p class="sidebar-empty">Could not load review.</p>';
      GH.flashError(error.message);
    });
  }

  function initDetail() {
    var deleteBtn = document.getElementById("delete-review");
    var id = reviewId();
    if (!document.getElementById("review-detail")) return;
    loadDetail();
    document.getElementById("finding-severity").addEventListener("change", function () { loadFindings(id); });
    document.getElementById("finding-category").addEventListener("change", function () { loadFindings(id); });
    document.getElementById("finding-confidence").addEventListener("change", function () { loadFindings(id); });
    document.getElementById("finding-addressed").addEventListener("change", function () { loadFindings(id); });
    document.addEventListener("click", function (event) {
      var toggle = event.target.closest(".finding-toggle");
      if (toggle) {
        GH.api("/reviews/api/reviews/findings/" + toggle.getAttribute("data-id"), {
          method: "PATCH",
          headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ addressed: toggle.getAttribute("data-addressed") === "1" }),
        }).then(function () {
          loadFindings(id);
        }).catch(function (error) { GH.flashError(error.message); });
      }
    });
    if (deleteBtn) {
      deleteBtn.addEventListener("click", function () {
        if (!window.confirm("Delete this review and its findings?")) return;
        GH.api("/reviews/api/reviews/" + id, { method: "DELETE" })
          .then(function () { window.location.href = "/reviews/"; })
          .catch(function (error) { GH.flashError(error.message); });
      });
    }
  }

  // ----- Project reviews -------------------------------------------------

  function runReview(kind, statusEl) {
    var pid = projectId();
    statusEl.hidden = false;
    statusEl.textContent = "Running " + kind + " review...";
    GH.api("/reviews/api/reviews", {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify({ source: "project", project_id: pid, kind: kind }),
    }).then(function (review) {
      statusEl.textContent = "";
      statusEl.hidden = true;
      if (review.status === "failed") {
        GH.flashError("Review failed: " + (review.error_message || "unknown error"));
      } else {
        window.location.href = "/reviews/" + review.id;
      }
    }).catch(function (error) {
      statusEl.hidden = false;
      statusEl.textContent = "";
      GH.flashError(error.message);
    });
  }

  function initProject() {
    var pid = projectId();
    var listEl = document.getElementById("review-list");
    if (!listEl) return;
    var metricsEl = document.getElementById("metrics-strip");
    loadMetrics(metricsEl);
    loadReviewList(listEl, { project_id: pid });
    var statusEl = document.getElementById("run-status");
    document.querySelectorAll(".review-run-buttons .btn").forEach(function (button) {
      button.addEventListener("click", function () {
        runReview(button.getAttribute("data-kind"), statusEl);
      });
    });
  }

  // ----- Config ----------------------------------------------------------

  function initConfig() {
    var pid = projectId();
    var saveBtn = document.getElementById("save-config");
    if (!saveBtn) return;
    GH.api("/reviews/api/projects/" + pid + "/config").then(function (config) {
      document.getElementById("cfg-enabled").value = config.enabled ? "1" : "0";
      var kinds = (config.kinds || "").split(",").map(function (k) { return k.trim(); });
      document.querySelectorAll("#cfg-kinds input[type=checkbox]").forEach(function (box) {
        box.checked = kinds.indexOf(box.value) >= 0;
      });
      document.getElementById("cfg-severity").value = config.severity_threshold || "low";
      document.getElementById("cfg-languages").value = config.languages || "";
      document.getElementById("cfg-security-focus").checked = !!config.security_focus;
      document.getElementById("cfg-performance-focus").checked = !!config.performance_focus;
      document.getElementById("cfg-testing-focus").checked = !!config.testing_focus;
      document.getElementById("cfg-max-files").value = config.max_files || "";
      document.getElementById("cfg-max-context").value = config.max_context_chars || "";
    }).catch(function (error) {
      GH.flashError(error.message);
    });

    saveBtn.addEventListener("click", function () {
      var kinds = [];
      document.querySelectorAll("#cfg-kinds input[type=checkbox]:checked").forEach(function (box) {
        kinds.push(box.value);
      });
      var payload = {
        enabled: document.getElementById("cfg-enabled").value === "1",
        kinds: kinds.join(","),
        severity_threshold: document.getElementById("cfg-severity").value,
        languages: document.getElementById("cfg-languages").value.trim(),
        security_focus: document.getElementById("cfg-security-focus").checked,
        performance_focus: document.getElementById("cfg-performance-focus").checked,
        testing_focus: document.getElementById("cfg-testing-focus").checked,
        max_files: parseInt(document.getElementById("cfg-max-files").value, 10),
        max_context_chars: parseInt(document.getElementById("cfg-max-context").value, 10),
      };
      var statusEl = document.getElementById("save-status");
      statusEl.textContent = "Saving...";
      GH.api("/reviews/api/projects/" + pid + "/config", {
        method: "PATCH",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify(payload),
      }).then(function () {
        statusEl.textContent = "Saved.";
        setTimeout(function () { statusEl.textContent = ""; }, 3000);
      }).catch(function (error) {
        statusEl.textContent = "";
        GH.flashError(error.message);
      });
    });
  }

  document.addEventListener("DOMContentLoaded", function () {
    if (document.getElementById("review-list") && !projectId()) {
      initIndex();
    } else if (document.getElementById("review-detail")) {
      initDetail();
    } else if (document.getElementById("review-list") && projectId()) {
      initProject();
    } else if (document.getElementById("save-config")) {
      initConfig();
    }
  });
})();
