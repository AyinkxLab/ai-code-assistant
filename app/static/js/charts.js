/**
 * Dependency-free dashboard charts for AI Code Assistant.
 * Renders simple time-series and breakdown charts from API data using vanilla JS Canvas API.
 * Handles small and large ranges gracefully.
 */

(() => {
  "use strict";

  /**
   * Color palette for charts (accessible, colorblind-friendly)
   */
  const CHART_COLORS = [
    "#3b82f6", // blue
    "#22c55e", // green
    "#f59e0b", // amber
    "#ef4444", // red
    "#8b5cf6", // violet
    "#ec4899", // pink
    "#06b6d4", // cyan
    "#84cc16", // lime
  ];

  /**
   * Format a number with appropriate suffix (K, M, B).
   *
   * Whole thousands/millions/billions drop the trailing ".0" ("1K", not
   * "1.0K"); fractional values keep one decimal ("1.5K").
   */
  function formatNumber(num) {
    function withSuffix(value, suffix) {
      const formatted = value.toFixed(1);
      return (formatted.endsWith(".0") ? formatted.slice(0, -2) : formatted) + suffix;
    }
    if (num >= 1e9) return withSuffix(num / 1e9, "B");
    if (num >= 1e6) return withSuffix(num / 1e6, "M");
    if (num >= 1e3) return withSuffix(num / 1e3, "K");
    return num.toString();
  }

  /**
   * Format a timestamp for display
   */
  function formatTime(timestamp, isShort = false) {
    const date = new Date(timestamp);
    if (isShort) {
      return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
    }
    return date.toLocaleDateString([], { month: "short", day: "numeric", hour: "2-digit", minute: "2-digit" });
  }

  /**
   * Calculate nice round numbers for axis scaling
   */
  function niceRange(min, max, steps = 5) {
    if (min === max) {
      max = min + 1;
    }
    const range = max - min;
    const step = Math.pow(10, Math.floor(Math.log10(range / steps)));
    const stepSize = Math.ceil(range / step / steps) * step;
    const newMin = Math.floor(min / stepSize) * stepSize;
    const newMax = Math.ceil(max / stepSize) * stepSize;
    return { min: newMin, max: newMax, step: stepSize };
  }

  /**
   * TimeSeriesChart - renders a line chart for time-series data
   * @param {HTMLCanvasElement} canvas - The canvas element to draw on
   * @param {Object} options - Chart options
   */
  class TimeSeriesChart {
    constructor(canvas, options = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.data = options.data || [];
      this.colors = options.colors || CHART_COLORS;
      this.lineWidth = options.lineWidth || 2;
      this.pointRadius = options.pointRadius || 3;
      this.showPoints = options.showPoints !== false;
      this.showGrid = options.showGrid !== false;
      this.showLegend = options.showLegend !== false;
      this.padding = options.padding || { top: 20, right: 20, bottom: 40, left: 60 };
      this.maxPoints = options.maxPoints || 100;

      // Handle high DPI displays
      this.resize();
      window.addEventListener("resize", () => this.resize());

      if (this.data.length > 0) {
        this.render();
      }
    }

    resize() {
      const dpr = window.devicePixelRatio || 1;
      const rect = this.canvas.getBoundingClientRect();
      this.canvas.width = rect.width * dpr;
      this.canvas.height = rect.height * dpr;
      this.ctx.scale(dpr, dpr);
      this.width = rect.width;
      this.height = rect.height;
      if (this.data.length > 0) {
        this.render();
      }
    }

    setData(data) {
      this.data = data;
      this.render();
    }

    render() {
      const ctx = this.ctx;
      const w = this.width;
      const h = this.height;
      const p = this.padding;
      const plotW = w - p.left - p.right;
      const plotH = h - p.top - p.bottom;

      if (plotW <= 0 || plotH <= 0) return;

      // Clear canvas
      ctx.clearRect(0, 0, w, h);

      // Limit data points for performance
      const displayData = this.data.slice(-this.maxPoints);

      // Find min/max values
      let minVal = Infinity;
      let maxVal = -Infinity;
      for (const series of displayData) {
        for (const point of series.values) {
          if (point.value < minVal) minVal = point.value;
          if (point.value > maxVal) maxVal = point.value;
        }
      }

      if (minVal === Infinity) return;

      // Nice axis range
      const range = niceRange(minVal, maxVal);

      // Time range
      const times = displayData.flatMap((s) => s.values.map((v) => v.timestamp));
      const minTime = Math.min(...times);
      const maxTime = Math.max(...times);
      const timeRange = maxTime - minTime || 1;

      // Draw grid
      if (this.showGrid) {
        this.drawGrid(ctx, plotW, plotH, p, range);
      }

      // Draw axes
      this.drawAxes(ctx, plotW, plotH, p, range, minTime, maxTime);

      // Draw series
      for (let i = 0; i < displayData.length; i++) {
        const series = displayData[i];
        const color = this.colors[i % this.colors.length];
        this.drawSeries(ctx, series, color, plotW, plotH, p, range, minTime, maxTime);
      }

      // Draw legend
      if (this.showLegend && displayData.length > 1) {
        this.drawLegend(ctx, displayData, w, h, p);
      }
    }

    drawGrid(ctx, plotW, plotH, p, range) {
      ctx.strokeStyle = "#e5e7eb";
      ctx.lineWidth = 1;

      // Horizontal grid lines
      const steps = Math.ceil((range.max - range.min) / range.step);
      for (let i = 0; i <= steps; i++) {
        const y = p.top + plotH - (i * range.step - (range.min - range.max) / (steps * range.step)) * (plotH / steps);
        ctx.beginPath();
        ctx.moveTo(p.left, y);
        ctx.lineTo(p.left + plotW, y);
        ctx.stroke();
      }

      // Vertical grid lines (time-based)
      const timeSteps = 5;
      for (let i = 0; i <= timeSteps; i++) {
        const x = p.left + (i / timeSteps) * plotW;
        ctx.beginPath();
        ctx.moveTo(x, p.top);
        ctx.lineTo(x, p.top + plotH);
        ctx.stroke();
      }
    }

    drawAxes(ctx, plotW, plotH, p, range, minTime, maxTime) {
      ctx.strokeStyle = "#9ca3af";
      ctx.lineWidth = 1;
      ctx.font = "11px system-ui, sans-serif";
      ctx.fillStyle = "#6b7280";
      ctx.textAlign = "right";
      ctx.textBaseline = "middle";

      // Y-axis
      ctx.beginPath();
      ctx.moveTo(p.left, p.top);
      ctx.lineTo(p.left, p.top + plotH);
      ctx.stroke();

      const steps = Math.ceil((range.max - range.min) / range.step);
      for (let i = 0; i <= steps; i++) {
        const value = range.min + (i * (range.max - range.min)) / steps;
        const y = p.top + plotH - (i / steps) * plotH;
        ctx.fillText(formatNumber(value), p.left - 8, y);
      }

      // X-axis
      ctx.beginPath();
      ctx.moveTo(p.left, p.top + plotH);
      ctx.lineTo(p.left + plotW, p.top + plotH);
      ctx.stroke();

      ctx.textAlign = "center";
      ctx.textBaseline = "top";
      const timeSteps = 5;
      for (let i = 0; i <= timeSteps; i++) {
        const x = p.left + (i / timeSteps) * plotW;
        const timestamp = minTime + (i / timeSteps) * (maxTime - minTime);
        ctx.fillText(formatTime(timestamp, true), x, p.top + plotH + 4);
      }
    }

    drawSeries(ctx, series, color, plotW, plotH, p, range, minTime, maxTime) {
      const values = series.values;
      if (values.length < 2) return;

      ctx.strokeStyle = color;
      ctx.lineWidth = this.lineWidth;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";

      // Draw line
      ctx.beginPath();
      for (let i = 0; i < values.length; i++) {
        const point = values[i];
        const x = p.left + ((point.timestamp - minTime) / (maxTime - minTime || 1)) * plotW;
        const y = p.top + plotH - ((point.value - range.min) / (range.max - range.min || 1)) * plotH;
        if (i === 0) {
          ctx.moveTo(x, y);
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.stroke();

      // Draw points
      if (this.showPoints) {
        ctx.fillStyle = color;
        for (const point of values) {
          const x = p.left + ((point.timestamp - minTime) / (maxTime - minTime || 1)) * plotW;
          const y = p.top + plotH - ((point.value - range.min) / (range.max - range.min || 1)) * plotH;
          ctx.beginPath();
          ctx.arc(x, y, this.pointRadius, 0, Math.PI * 2);
          ctx.fill();
        }
      }
    }

    drawLegend(ctx, series, w, h, p) {
      ctx.font = "12px system-ui, sans-serif";
      ctx.textAlign = "left";
      ctx.textBaseline = "middle";

      const itemHeight = 20;
      const startY = p.top;
      const startX = w - p.right - 120;

      series.forEach((s, i) => {
        const color = this.colors[i % this.colors.length];
        const y = startY + i * itemHeight;

        // Color indicator
        ctx.fillStyle = color;
        ctx.beginPath();
        ctx.arc(startX - 8, y, 5, 0, Math.PI * 2);
        ctx.fill();

        // Label
        ctx.fillStyle = "#374151";
        ctx.fillText(s.label || s.id, startX, y);
      });
    }
  }

  /**
   * BreakdownChart - renders a horizontal bar chart for breakdown data
   * @param {HTMLCanvasElement} canvas - The canvas element to draw on
   * @param {Object} options - Chart options
   */
  class BreakdownChart {
    constructor(canvas, options = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.data = options.data || [];
      this.colors = options.colors || CHART_COLORS;
      this.padding = options.padding || { top: 20, right: 20, bottom: 20, left: 120 };
      this.barHeight = options.barHeight || 24;
      this.barGap = options.barGap || 8;
      this.showValues = options.showValues !== false;

      this.resize();
      window.addEventListener("resize", () => this.resize());

      if (this.data.length > 0) {
        this.render();
      }
    }

    resize() {
      const dpr = window.devicePixelRatio || 1;
      const rect = this.canvas.getBoundingClientRect();
      this.canvas.width = rect.width * dpr;
      this.canvas.height = rect.height * dpr;
      this.ctx.scale(dpr, dpr);
      this.width = rect.width;
      this.height = rect.height;
      if (this.data.length > 0) {
        this.render();
      }
    }

    setData(data) {
      this.data = data;
      this.render();
    }

    render() {
      const ctx = this.ctx;
      const w = this.width;
      const h = this.height;
      const p = this.padding;
      const plotW = w - p.left - p.right;
      const plotH = h - p.top - p.bottom;

      if (plotW <= 0 || plotH <= 0) return;

      // Clear canvas
      ctx.clearRect(0, 0, w, h);

      // Find max value
      let maxVal = 0;
      for (const item of this.data) {
        if (item.value > maxVal) maxVal = item.value;
      }
      if (maxVal === 0) maxVal = 1;

      // Calculate layout
      const itemHeight = this.barHeight + this.barGap;
      const startY = p.top;

      // Draw bars
      this.data.forEach((item, i) => {
        const y = startY + i * itemHeight;
        const barWidth = (item.value / maxVal) * plotW;
        const color = this.colors[i % this.colors.length];

        // Bar background
        ctx.fillStyle = color + "33"; // 20% opacity
        ctx.fillRect(p.left, y, plotW, this.barHeight);

        // Bar value
        ctx.fillStyle = color;
        ctx.fillRect(p.left, y, barWidth, this.barHeight);

        // Label
        ctx.font = "12px system-ui, sans-serif";
        ctx.fillStyle = "#374151";
        ctx.textAlign = "right";
        ctx.textBaseline = "middle";
        ctx.fillText(item.label || item.source || "Unknown", p.left - 12, y + this.barHeight / 2);

        // Value
        if (this.showValues) {
          ctx.textAlign = "left";
          ctx.fillText(formatNumber(item.value), p.left + barWidth + 8, y + this.barHeight / 2);
        }
      });

      // Draw total if provided
      if (this.total !== undefined) {
        ctx.font = "bold 13px system-ui, sans-serif";
        ctx.fillStyle = "#111827";
        ctx.textAlign = "left";
        ctx.textBaseline = "bottom";
        ctx.fillText(`Total: ${formatNumber(this.total)}`, p.left, h - p.bottom);
      }
    }

    setTotal(total) {
      this.total = total;
      this.render();
    }
  }

  /**
   * SparklineChart - tiny inline chart for quick trends
   */
  class SparklineChart {
    constructor(canvas, options = {}) {
      this.canvas = canvas;
      this.ctx = canvas.getContext("2d");
      this.data = options.data || [];
      this.color = options.color || CHART_COLORS[0];
      this.lineWidth = options.lineWidth || 1.5;
      this.fillColor = options.fillColor || this.color + "22";

      this.resize();
      window.addEventListener("resize", () => this.resize());

      if (this.data.length > 0) {
        this.render();
      }
    }

    resize() {
      const dpr = window.devicePixelRatio || 1;
      const rect = this.canvas.getBoundingClientRect();
      this.canvas.width = rect.width * dpr;
      this.canvas.height = rect.height * dpr;
      this.ctx.scale(dpr, dpr);
      this.width = rect.width;
      this.height = rect.height;
      if (this.data.length > 0) {
        this.render();
      }
    }

    setData(data) {
      this.data = data;
      this.render();
    }

    render() {
      const ctx = this.ctx;
      const w = this.width;
      const h = this.height;

      if (w <= 0 || h <= 0 || this.data.length < 2) return;

      // Clear
      ctx.clearRect(0, 0, w, h);

      // Find min/max
      let minVal = Infinity;
      let maxVal = -Infinity;
      for (const v of this.data) {
        if (v < minVal) minVal = v;
        if (v > maxVal) maxVal = v;
      }
      if (minVal === maxVal) {
        maxVal = minVal + 1;
      }

      // Draw filled area
      ctx.fillStyle = this.fillColor;
      ctx.beginPath();
      ctx.moveTo(0, h);
      for (let i = 0; i < this.data.length; i++) {
        const x = (i / (this.data.length - 1)) * w;
        const y = h - ((this.data[i] - minVal) / (maxVal - minVal)) * h;
        ctx.lineTo(x, y);
      }
      ctx.lineTo(w, h);
      ctx.closePath();
      ctx.fill();

      // Draw line
      ctx.strokeStyle = this.color;
      ctx.lineWidth = this.lineWidth;
      ctx.lineCap = "round";
      ctx.lineJoin = "round";
      ctx.beginPath();
      for (let i = 0; i < this.data.length; i++) {
        const x = (i / (this.data.length - 1)) * w;
        const y = h - ((this.data[i] - minVal) / (maxVal - minVal)) * h;
        if (i === 0) {
          ctx.moveTo(x, y);
        } else {
          ctx.lineTo(x, y);
        }
      }
      ctx.stroke();
    }
  }

  // Export to window
  window.AICodeAssistant = window.AICodeAssistant || {};
  window.AICodeAssistant.charts = {
    TimeSeriesChart,
    BreakdownChart,
    SparklineChart,
    formatNumber,
    formatTime,
  };
})();