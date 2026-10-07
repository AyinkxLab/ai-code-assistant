/* Usage dashboard: fetches the usage summary and renders a daily token chart. */
(function () {
  "use strict";

  var page = document.querySelector(".usage-page");
  if (!page) {
    return;
  }

  var summaryUrl = page.getAttribute("data-usage-summary-url");
  var canvas = document.getElementById("usage-chart");
  var emptyMsg = document.getElementById("usage-empty");

  function formatInt(value) {
    return Number(value || 0).toLocaleString();
  }

  function formatCost(value) {
    return "$" + Number(value || 0).toFixed(2);
  }

  function setText(id, text) {
    var el = document.getElementById(id);
    if (el) {
      el.textContent = text;
    }
  }

  function renderSummary(summary) {
    if (!summary) {
      return;
    }
    setText("usage-total-tokens", formatInt(summary.total_tokens));
    setText("usage-total-cost", formatCost(summary.estimated_cost));
    setText("usage-prompt-tokens", formatInt(summary.prompt_tokens));
    setText("usage-completion-tokens", formatInt(summary.completion_tokens));
  }

  function drawChart(daily) {
    if (!canvas || !canvas.getContext) {
      return;
    }
    var data = Array.isArray(daily) ? daily : [];
    var hasData = data.some(function (d) {
      return Number(d.total_tokens || 0) > 0;
    });
    if (!hasData) {
      if (emptyMsg) {
        emptyMsg.hidden = false;
      }
      canvas.hidden = true;
      return;
    }
    if (emptyMsg) {
      emptyMsg.hidden = true;
    }
    canvas.hidden = false;

    var ctx = canvas.getContext("2d");
    var width = canvas.width;
    var height = canvas.height;
    var padding = { top: 24, right: 24, bottom: 40, left: 64 };
    var plotWidth = width - padding.left - padding.right;
    var plotHeight = height - padding.top - padding.bottom;

    ctx.clearRect(0, 0, width, height);

    var maxValue = 0;
    data.forEach(function (d) {
      maxValue = Math.max(maxValue, Number(d.total_tokens || 0));
    });
    if (maxValue <= 0) {
      maxValue = 1;
    }
    maxValue = maxValue * 1.15;

    ctx.strokeStyle = "#2d3748";
    ctx.lineWidth = 1;
    ctx.font = "12px Inter, sans-serif";
    ctx.fillStyle = "#94a3b8";
    ctx.textAlign = "right";
    ctx.textBaseline = "middle";

    var gridLines = 4;
    for (var i = 0; i <= gridLines; i++) {
      var y = padding.top + (plotHeight * i) / gridLines;
      var value = Math.round((maxValue * (gridLines - i)) / gridLines);
      ctx.beginPath();
      ctx.moveTo(padding.left, y);
      ctx.lineTo(width - padding.right, y);
      ctx.stroke();
      ctx.fillText(formatInt(value), padding.left - 8, y);
    }

    var barCount = data.length;
    var slotWidth = plotWidth / Math.max(barCount, 1);
    var barWidth = Math.max(1, Math.floor(slotWidth * 0.6));

    data.forEach(function (d, index) {
      var total = Number(d.total_tokens || 0);
      var barHeight = (
        total / maxValue
      ) * plotHeight;
      var x = padding.left + index * slotWidth + (slotWidth - barWidth) / 2;
      var y = padding.top + plotHeight - barHeight;
      ctx.fillStyle = "#6366f1";
      ctx.fillRect(x, y, barWidth, barHeight);
    });

    ctx.strokeStyle = "#2d3748";
    ctx.beginPath();
    ctx.moveTo(padding.left, padding.top + plotHeight);
    ctx.lineTo(width - padding.right, padding.top + plotHeight);
    ctx.stroke();

    ctx.fillStyle = "#94a3b8";
    ctx.textAlign = "center";
    ctx.textBaseline = "top";
    var labelStep = Math.max(1, Math.ceil(barCount / 6));
    data.forEach(function (d, index) {
      if (index % labelStep !== 0 && index !== barCount - 1) {
        return;
      }
      var x = padding.left + index * slotWidth + slotWidth / 2;
      ctx.fillText(d.label || "", x, padding.top + plotHeight + 8);
    });
  }

  function load() {
    if (!summaryUrl) {
      return;
    }
    fetch(summaryUrl, {
      headers: { Accept: "application/json" },
      credentials: "same-origin",
    })
      .then(function (response) {
        if (!response.ok) {
          throw new Error("Usage summary request failed");
        }
        return response.json();
      })
      .then(function (payload) {
        renderSummary(payload.summary);
        drawChart(payload.daily);
      })
      .catch(function () {
        if (emptyMsg) {
          emptyMsg.hidden = false;
          emptyMsg.textContent = "Unable to load usage data.";
        }
      });
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", load);
  } else {
    load();
  }
})();
